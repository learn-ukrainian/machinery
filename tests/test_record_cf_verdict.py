"""Existing recorder invariants ported from the product file at the same path."""

import json
import os
import subprocess
import sys
from pathlib import Path

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

    # R08: an untokened Carbon marker is not idempotent with a binding second review.
    from lu_machinery.orchestration import integration_sweep as sweep
    from tests.review.record_support import facts_for
    task = json.loads((tasks / "review-one.json").read_text())
    task.update(model="gemini-4-carbon-medium", agent="agy")
    (tasks / "review-one.json").write_text(json.dumps(task))
    monkeypatch.setattr(recorder, "_pr", lambda *_: {
        "number": 1, "headRefOid": task["worktree_base_sha"], "headRefName": task["worktree_branch"],
    })
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **__: facts_for({"anthropic"}))
    _files[:] = [{"filename": ".github/workflows/ci.yml", "status": "modified"},
                 {"filename": "curriculum/l2-uk-en/a1/example.md", "status": "modified"}]
    marker_fields = dict(sha=task["worktree_base_sha"], task_id="review-one", started=task["started_at"],
                         verdict="APPROVED", model=task["model"], family="google", reply="VERDICT: APPROVE",
                         author_models=["claude-opus-5-5"])
    stamp = "2026-09-23T13:00:00Z"
    comments.append({"body": recorder.build_comment(**marker_fields), "user": {"login": "fleet"},
                     "author_association": "MEMBER", "created_at": stamp, "updated_at": stamp})
    with pytest.raises(recorder.RecordError, match="conflicts with task"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert len(comments) == 1 and statuses == []

    # R08: after both code seats approve, missing learner seats keep the status pending.
    comments[0]["body"] = recorder.build_comment(**{**marker_fields, "task_id": "carbon", "second_review": True})
    task.update(model="gpt-6.1-sol", agent="codex")
    (tasks / "review-one.json").write_text(json.dumps(task))
    # This fixture's product-independent policy must be shared with the recorder as well.
    monkeypatch.setattr(sweep, "changes_learner_content", lambda paths: any(p.startswith("curriculum/") for p in paths))
    result = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED" and statuses[-1]["state"] == "pending"
    assert sweep.lookup_verdict(comments, task["worktree_base_sha"], "fleet", second_review=True,
                                learner_content=True).state == "needs-seat"
    with pytest.raises(recorder.RecordError, match="not the catalog"):
        recorder._require_second_reviewer(facts_for({"anthropic"}), task=task, model="gemini-4-carbon-low",
                                          family="google", review_mode="cross_family", required=True)
    assert "-m lu_machinery.review.record_cf_verdict" in recorder.RECORDER_COMMAND
    # The shared store override wins even when Git's primary belongs to machinery.
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(recorder, "_repo_root", lambda: tmp_path / "other-primary")
    retry = recorder.record("review-one", pr_number=1, lock_root=tmp_path / "locks")
    assert retry["verdict"] == "APPROVED" and len(comments) == 2
    # A historical deletion must retain its Low tier for same-family approval accounting.
    monkeypatch.setattr(recorder, "default_branch_review_base", lambda *_, **__: ("b" * 40, "b" * 40))
    monkeypatch.setattr(recorder, "_facts_git", lambda *_, **__: b"D\0lu_machinery/common/unit.py\0")
    context = recorder.head_approval_context(tmp_path, "a" * 40, repository="owner/repo", fetch=False)
    assert context == {"second_review": False, "repository": "owner/repo", "learner_content": False, "low_tier": True}
    # A missing product policy cannot crash after publication or waive learner seats.
    monkeypatch.setattr(sweep, "changes_learner_content",
                        lambda _: (_ for _ in ()).throw(sweep.DeployPathsError("policy unavailable")))
    unknown = recorder.record("review-one", pr_number=1, lock_root=tmp_path / "locks")
    assert unknown["verdict"] == "APPROVED" and statuses[-1]["state"] == "pending" and len(comments) == 2
    assert recorder.head_approval_context(tmp_path, "a" * 40, repository="owner/repo", fetch=False)["learner_content"] is True
    # Default locks accompany the shared task store, independent of target Git discovery.
    from shutil import copytree
    shared = tmp_path / "shared-state" / "batch_state" / "tasks"
    copytree(tasks, shared)
    monkeypatch.setenv("LU_TASKS_DIR", str(shared))
    recorder.record("review-one", pr_number=1)
    assert (shared.parent / "locks").is_dir()
    assert not (tmp_path / "other-primary" / "batch_state" / "locks").exists()






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
    assert repair_round.failed_runs_for(approved, **kwargs, workflows=repair_round.required_workflows(REPOSITORY)) == []
    gate_queue = [{**item, "path": gate} for item in queue]
    found = repair_round.failed_runs_for(
        approved, **{**kwargs, "github": fake_github_reads(queue=gate_queue, trees={"c" * 40: tree})}, workflows=(gate,)
    )
    assert found == [{"run_id": 8, "event": "merge_group", "conclusion": "failure"}]
    # A list normalizes to the same collection; each listed workflow's failure is evidence on its own.
    other = ".github/workflows/other.yml"
    monkeypatch.setattr(
        repair_round.prepublish, "catalog", lambda: {"unit": {"github": REPOSITORY, "required_workflow": [gate, other]}}
    )
    listed = repair_round.required_workflows(REPOSITORY)
    assert listed == (gate, other)
    for index, workflow in enumerate(listed):
        both = [{**item, "path": workflow} for item in queue]
        found = repair_round.failed_runs_for(
            approved, **{**kwargs, "github": fake_github_reads(queue=both, trees={"c" * 40: tree})}, workflows=listed
        )
        assert found == [{"run_id": 8, "event": "merge_group", "conclusion": "failure"}], index
    unrelated = [{**item, "path": ".github/workflows/unrelated.yml"} for item in queue]
    unrelated_reads = fake_github_reads(queue=unrelated, trees={"c" * 40: tree})
    assert repair_round.failed_runs_for(approved, **{**kwargs, "github": unrelated_reads}, workflows=listed) == []
    bad_rows = (
        "ci.yml",
        1,
        [],
        [gate, 1],
        [gate, "ci.yml"],
        [gate, gate],
        [[gate]],
        {"path": gate},
    )
    for bad_value in bad_rows:
        bad = {"github": REPOSITORY, "required_workflow": bad_value}
        monkeypatch.setattr(repair_round.prepublish, "catalog", lambda bad=bad: {"unit": bad})
        with pytest.raises(repair_round.RepairRefused, match=r"\[evidence\].*required_workflow"):
            repair_round.required_workflows(REPOSITORY)
    monkeypatch.setattr(repair_round.prepublish, "catalog", lambda: {"a": row, "b": row})
    with pytest.raises(repair_round.RepairRefused, match=r"\[evidence\].*registered more than once"):
        repair_round.required_workflows(REPOSITORY)
    # Every registered repository without the key keeps ci.yml; machinery names ci-gate.yml and kube-dojo its four.
    monkeypatch.undo()
    workflows = {key: repair_round.required_workflows(repo.github) for key, repo in recorder.load_fleet_repos().items()}
    assert workflows.pop("machinery") == (gate,)
    assert workflows.pop("kube-dojo") == tuple(
        f".github/workflows/{name}.yml" for name in ("incident-dedup", "link-check", "build-check", "vitest-check")
    )
    assert set(workflows.values()) == {(repair_round.DEFAULT_REQUIRED_WORKFLOW,)}


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


@pytest.mark.parametrize("failure", ["missing", "linked", "symlink-loop"])
@pytest.mark.parametrize("form", ["file", "module"])
@pytest.mark.parametrize("entry", ["review.record_cf_verdict", "dispatch", "dispatcher.cli"])
def test_installed_entry_refuses_invalid_configured_primary_before_dependencies(tmp_path, entry, form, failure):
    """Defect class: an unusable configured primary escaping the bootstrap as a traceback or loading dependencies.

    The recorder keeps its typed RECORDER_PRIMARY_REFUSED (exit 2, empty stderr); the
    dispatcher entries print their one ``dispatcher:`` reason line (exit 2).
    """
    site = Path(recorder.__file__).resolve().parents[2]
    module = f"lu_machinery.{entry}"
    if failure == "missing":
        primary = tmp_path / "missing-primary"
    elif failure == "linked":
        primary = tmp_path / "linked-worktree"
        primary.mkdir()
        (primary / ".git").write_text(f"gitdir: {tmp_path / 'primary/.git/worktrees/linked'}\n")
    else:
        primary, second = tmp_path / "loop-a", tmp_path / "loop-b"
        primary.symlink_to(second)
        second.symlink_to(primary)
    # The bootstrap entries refuse before any dependency loads; dispatcher.cli imports its own at module top.
    poisoned = tmp_path / "poisoned"
    poisoned.mkdir()
    if entry != "dispatcher.cli":
        (poisoned / "yaml.py").write_text("raise RuntimeError('dependency loaded before the refusal')\n")
    target = [str(site / module.replace(".", "/")) + ".py"] if form == "file" else ["-m", module]
    argv = ["budget-check", "--repository", "owner/repo", "--pr", "42"] if entry.startswith("review") else ["list"]
    done = subprocess.run(
        [sys.executable, *target, *argv],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(poisoned), str(site)]),
             "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(primary)},
        text=True, capture_output=True, check=False, timeout=60,
    )
    reason = f"LEARN_UKRAINIAN_PRIMARY_REPO_ROOT must name a primary checkout, not a linked worktree or a stray .git: {primary}/.git"
    assert done.returncode == 2, done.stderr
    if entry.startswith("review"):
        assert done.stderr == ""
        [line] = done.stdout.splitlines()
        refusal = json.loads(line)
        assert refusal["admitted"] is False
        assert refusal["refusal"].startswith(f"RECORDER_PRIMARY_REFUSED: {reason}")
    else:
        assert done.stdout == ""
        [line] = done.stderr.splitlines()
        assert line.startswith(f"dispatcher: {reason}")

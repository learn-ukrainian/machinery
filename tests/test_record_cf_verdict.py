"""Existing recorder invariants ported from the product file at the same path."""



import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review import repair_round
from tests.review.record_support import write_task as write_task
import time
from lu_machinery.orchestration import integration_sweep as sweep
from lu_machinery.orchestration import merge_queue_keeper as keeper
from lu_machinery.review.ukrainian_seats import UKRAINIAN_SEAT_FAMILIES
from lu_machinery.review.ukrainian_seats import attested_author_families
from lu_machinery.review.ukrainian_seats import required_ukrainian_seats
from tests.review.record_support import SHA as SHA, setup_record as _setup_record
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.agent_runtime.product_roots import agent_runtime_roots as agent_runtime_roots
from tests.helpers import current_sol

CURRENT_SOL = current_sol()


def attested_payload(explanation="No defect found at the reviewed head.", findings=(), *, attestation="SECURITY_VERDICT: PASS"):
    """An approving findings object whose ``overall.explanation`` opens with the security sign-off line."""
    if attestation is not None:
        explanation = f"{attestation}\n{explanation}"
    return {
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": explanation, "confidence": 0.9},
        "findings": list(findings),
    }


def attested_approval(*args, **kwargs):
    """The sealed reply of an approval with the sign-off, as the schema-bound worker writes it (``structured_result``)."""
    return json.dumps(attested_payload(*args, **kwargs), ensure_ascii=False, allow_nan=False)


def approve_envelope(payload):
    """A review-loop envelope stating APPROVE around ``payload``."""
    return "\n".join(("VERDICT: APPROVE", "SCORE: 9/10", "", "```json", json.dumps(payload, indent=2), "```", ""))


@pytest.mark.usefixtures("configured_roots", "agent_runtime_roots", "recorder_matcher")
@pytest.mark.parametrize("rule", [
    "item-quote", "evidence-quote", "evidence-receipt", "unknown-source", "source-excerpt",
    "finding-source", "bookkeeping", "unknown-coverage", "record-unknown", "record-excerpt",
])
def test_ukrainian_reference_and_recorder_agree_on_item_rules(rule, monkeypatch, tmp_path):
    """Defect class: reviewer instructions and recorder acceptance drift apart."""
    from lu_machinery.review.review_contract import (
        build_item_packet,
        judge_item_reply,
        render_item_packet,
        validate_ukrainian_reply_claims,
    )

    reference = (Path(__file__).resolve().parents[1] / "agents_extensions/shared/skills/review-loop/references/ukrainian.md").read_text()
    phrases = {
        "item-quote": "row-level `quote` and `quotes` fields quote the frozen item",
        "evidence-quote": "`quote` inside an item `evidence` entry quotes captured Sources output",
        "evidence-receipt": "Every item `evidence` entry must have a receipt id (`receipt_id`) and a non-empty `quote`",
        "unknown-source": "Source-claim checks also apply to UNKNOWN rows in an APPROVED mixed reply",
        "source-excerpt": "each excerpt must be literal captured Sources output",
        "finding-source": "`finding` clause naming a source term",
        "bookkeeping": "coverage set in the reply void the chunk",
        "unknown-coverage": "coverage equals the denominator",
        "record-unknown": "an unsupported source claim in an UNKNOWN row refuses the whole reply",
        "record-excerpt": "On an APPROVE verdict, validated Ukrainian `sources` excerpts are checked against captured output instead",
    }
    assert phrases[rule] in " ".join(reference.split())
    packet = build_item_packet([{"id": "a", "text": "learner text"}, {"id": "b", "text": "other text"}])
    rows = [
        {"id": "a", "verdict": "APPROVE", "finding": "none", "sources": "none"},
        {"id": "b", "verdict": "UNKNOWN", "finding": "none", "sources": "none"},
    ]
    payload = {"schema": "ukrainian-item-judgments.v1", "items": rows}
    event = {"receipt_id": "r1", "tool": "verify_word", "arguments": {}, "result": "captured output"}
    if rule in {"record-unknown", "record-excerpt"}:
        tasks, comments, calls = setup_record(monkeypatch, tmp_path)
        prompt = tmp_path / "brief.md"
        prompt.write_text(render_item_packet(packet))
        captured_source_trace(tasks, output='captured "source excerpt"')
        if rule == "record-unknown":
            rows[1]["sources"] = "unsupported source claim"
        else:
            rows[0]["sources"] = 'captured "source excerpt"'
        reply = "VERDICT: APPROVE\n```ukrainian-item-judgments\n" + json.dumps(payload) + "\n```\n"
        write_task(tasks, reply=reply, review_profile="ukrainian", sources_mcp_call_count=3,
                   prompt_file=str(prompt), prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest())
        if rule == "record-unknown":
            with pytest.raises(recorder.RecordError, match="UKRAINIAN_CLAIMS_UNSUPPORTED"):
                recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
            assert comments == [] and calls["posts"] == 0 and calls["statuses"] == 0
        else:
            recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
            marker = sweep.parse_marker(comments[0]["body"])
            assert marker.get("void") is None and marker["coverage"] == ["a"]
        return
    if rule == "item-quote":
        rows[0]["quote"] = "not in item"
    elif rule == "evidence-quote":
        rows[0]["evidence"] = {"receipt": "r1", "quote": "captured output"}
    elif rule == "evidence-receipt":
        rows[0]["evidence"] = {"receipt": "r1"}
    elif rule == "unknown-source":
        rows[1]["sources"] = "uncaptured output"
    elif rule == "source-excerpt":
        rows[0]["sources"] = "uncaptured output"
    elif rule == "finding-source":
        rows[0]["finding"] = "VESUM says this"
    elif rule == "bookkeeping":
        payload["coverage"] = ["a"]
    reply = "VERDICT: APPROVE\n```ukrainian-item-judgments\n" + json.dumps(payload) + "\n```\n"
    sanitized, claims = validate_ukrainian_reply_claims(reply, tool_events=[event])
    if rule in {"evidence-receipt", "unknown-source", "source-excerpt", "finding-source"}:
        assert claims.claims_unsupported == 1
    else:
        assert claims.claims_unsupported == 0
        chunk = judge_item_reply(packet, sanitized, stated="APPROVED", ukrainian=True, source_claims_validated=True)
        if rule == "item-quote":
            assert chunk.void == "quote-mismatch" and chunk.coverage == ()
        elif rule == "bookkeeping":
            assert chunk.void == "reviewer-bookkeeping" and chunk.coverage == ()
        else:
            assert chunk.void is None and chunk.coverage == ("a",)


def test_historical_admission_read_survives_a_listed_record_moving(tmp_path, monkeypatch):
    """Defect class: archiving a listed record refuses the recorder's historical scan."""
    hot = tmp_path / "retired.json"
    archived = tmp_path / "archive" / hot.name
    archived.parent.mkdir()
    hot.write_text(json.dumps({"task_id": "retired", "schema_version": 2, "status": "done"}))
    read_bytes = Path.read_bytes

    def archive_before_read(path):
        if path == hot and hot.exists():
            hot.rename(archived)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", archive_before_read)
    from lu_machinery.orchestration import task_record_store

    read_regular = task_record_store.read_regular_bytes

    def archive_before_regular_read(path):
        if path == hot and hot.exists():
            hot.rename(archived)
        return read_regular(path)

    monkeypatch.setattr(task_record_store, "read_regular_bytes", archive_before_regular_read)

    assert recorder.admitted_unknown_history(
        repository="org/repo", branch="codex/topic", head_sha=SHA, task_root=tmp_path, repo_root=tmp_path,
    ) == ()
    assert archived.is_file()


def test_in_flight_guard_survives_a_listed_terminal_review_moving(tmp_path, monkeypatch):
    """Defect class: a terminal review moving to archive looks like an unavailable in-flight ledger."""
    hot = tmp_path / "retired.json"
    archived = tmp_path / "archive" / hot.name
    archived.parent.mkdir()
    hot.write_text(json.dumps({
        "task_id": "retired", "schema_version": 2, "status": "done", "review": True,
        "repository": "org/repo", "pinned_head": SHA,
    }))
    read_bytes = Path.read_bytes

    def archive_before_read(path):
        if path == hot and hot.exists():
            hot.rename(archived)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", archive_before_read)
    from lu_machinery.orchestration import task_record_store

    read_regular = task_record_store.read_regular_bytes

    def archive_before_regular_read(path):
        if path == hot and hot.exists():
            hot.rename(archived)
        return read_regular(path)

    monkeypatch.setattr(task_record_store, "read_regular_bytes", archive_before_regular_read)

    recorder.require_no_outside_review_in_flight(tmp_path, "org/repo", SHA)
    assert archived.is_file()


def test_repair_receipts_stay_private_on_admission_and_consumption(tmp_path, monkeypatch):
    """Defect class: receipt replacement leaks evidence, leaves partial files or fails on stale PID temps."""
    receipt = {
        "schema": repair_round.RECEIPT_SCHEMA,
        "repository": "example/public",
        "pr": 1,
        "lineage_keys": ["pr:example/public#1"],
        "approved_head": "a" * 40,
        "candidate_head": "b" * 40,
    }
    previous = os.umask(0)
    try:
        repair_round.record_admission(tmp_path, receipt)
        path, = tmp_path.glob("*.json")
        assert path.stat().st_mode & 0o777 == 0o600
        stale = tmp_path / f".{path.name}.{os.getpid()}.tmp"
        stale.write_text("unrelated stale fixture")

        def failed_replace(source, destination):
            assert Path(source).stat().st_mode & 0o777 == 0o600
            raise OSError("replace failure fixture")

        with monkeypatch.context() as patch:
            patch.setattr(repair_round.os, "replace", failed_replace)
            with pytest.raises(OSError, match="replace failure fixture"):
                repair_round.record_consumption(tmp_path, receipt, comment="fixture")
        assert json.loads(path.read_text())["state"] == "admitted"
        assert set(tmp_path.iterdir()) == {path, stale}
        repair_round.record_consumption(tmp_path, receipt, comment="https://github.com/example/public/pull/1#issuecomment-1")
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["state"] == "consumed"
        assert set(tmp_path.iterdir()) == {path, stale}
        assert stale.read_text() == "unrelated stale fixture"
    finally:
        os.umask(previous)

    old, current = "learn-ukrainian/machinery", "agentic-machinery/machinery"
    branch = "codex/example"
    transferred = tmp_path / "transferred"
    legacy = {
        **receipt,
        "repository": old,
        "lineage_keys": [f"pr:{old}#1", f"branch:{old}:{branch}", f"issue:{old}#7"],
    }
    repair_round.record_admission(transferred, legacy)
    keys = repair_round.lineage_keys(current, 1, branch, [7])
    assert keys == repair_round.lineage_keys(old, 1, branch, [7])
    assert {"pr:1445647449#1", f"branch:1445647449:{branch}", "issue:1445647449#7"} <= set(keys)
    assert set(legacy["lineage_keys"]) <= set(keys)
    assert repair_round.repair_heads(transferred, repository=current, number=1, branch=branch) == {"b" * 40}
    prior = repair_round.receipt_at(transferred, repository=current, number=1, branch=branch, head="b" * 40)
    assert prior is not None and prior["repository"] == old
    current_receipt = {**legacy, "repository": current, "lineage_keys": keys}
    with pytest.raises(repair_round.RepairRefused, match="took its repair round concurrently"):
        repair_round.record_admission(transferred, {**current_receipt, "candidate_head": "c" * 40})
    assert len(list(transferred.glob("*.json"))) == len(legacy["lineage_keys"])
    repair_round.record_consumption(transferred, legacy, comment="fixture")
    repair_round.record_admission(transferred, current_receipt)
    assert all(repair_round._receipt_path(transferred, key).is_file() for key in keys)
    prior = repair_round.receipt_at(transferred, repository=current, number=1, branch=branch, head="b" * 40)
    assert prior is not None and prior["state"] == "consumed"


def test_same_family_refused(monkeypatch, low_review, tmp_path):
    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": "lu_machinery/common/example.py", "status": "modified"}]
    with pytest.raises(recorder.RecordError, match="equals an author family"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []


@pytest.mark.parametrize("model,family", [("claude-opus-5-5", "anthropic"), (CURRENT_SOL, "openai")])
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
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert len(comments) == 1 and statuses == []

    # R08: after both code seats approve, missing learner seats keep the status pending.
    comments[0]["body"] = recorder.build_comment(**{**marker_fields, "task_id": "carbon", "second_review": True})
    task.update(model=CURRENT_SOL, agent="codex")
    (tasks / "review-one.json").write_text(json.dumps(task))
    # This fixture's product-independent policy must be shared with the recorder as well.
    monkeypatch.setattr(sweep, "changes_learner_content", lambda paths: any(p.startswith("curriculum/") for p in paths))
    result = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED" and statuses[-1]["state"] == "pending"
    assert sweep.lookup_verdict(comments, task["worktree_base_sha"], "fleet", second_review=True,
                                learner_content=True).state == "needs-seat"
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
    row = {"github": REPOSITORY.upper(), "default": True, "required_workflow": gate}
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
        repair_round.prepublish, "catalog", lambda: {"unit": {"github": REPOSITORY, "default": True, "required_workflow": [gate, other]}}
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
        bad = {"github": REPOSITORY, "default": True, "required_workflow": bad_value}
        monkeypatch.setattr(repair_round.prepublish, "catalog", lambda bad=bad: {"unit": bad})
        with pytest.raises(repair_round.RepairRefused, match=r"\[evidence\].*required_workflow"):
            repair_round.required_workflows(REPOSITORY)
    monkeypatch.setattr(repair_round.prepublish, "catalog", lambda: {"a": row, "b": {**row, "default": False}})
    with pytest.raises(repair_round.RepairRefused, match=r"\[evidence\].*registered more than once"):
        repair_round.required_workflows(REPOSITORY)


def test_every_registered_row_requires_exactly_its_repository_merge_gates(monkeypatch):
    """Defect classes: a fleet_repos.yaml row whose required workflows (the ci.yml default included) differ from
    the workflows that gate that repository's merges, so a repair round refuses a real CI failure or accepts a
    non-gating one; and a merge-queue row naming several workflows, which merge closeout refuses.

    The gates come from a fixture read from GitHub: the workflows behind the branch's required checks, or, where
    GitHub requires none, every workflow that runs on pull requests (the publisher refuses any failing check).
    """
    import yaml

    from lu_machinery.common import git_credentials

    document = yaml.safe_load((Path(__file__).parent / "review/fixtures/fleet_repo_workflows.yaml").read_text())
    assert isinstance(document, dict) and isinstance(document.get("repos"), dict)
    recorded = document["repos"]
    registered = recorder.load_fleet_repos()
    # A transferred row resolves only through a host selector; the test owns one, never the host's.
    selected = {key: repo.github.lower() for key, repo in registered.items() if repo.alternate_github}
    monkeypatch.setattr(git_credentials, "load_selector", lambda **_kwargs: git_credentials.SelectorState(selected, "d" * 64))
    # A newly registered repository has no recorded gates until someone reads them from GitHub.
    assert set(recorded) == set(registered)
    wrong = {}
    for key, repo in registered.items():
        row = recorded[key]
        lists = [row.get(name) if isinstance(row, dict) else None for name in ("checks", "pull_request")]
        if not (
            isinstance(row, dict)
            and set(row) == {"merge_queue", "checks", "pull_request"}
            and type(row["merge_queue"]) is bool
            and all(isinstance(names, list) and all(isinstance(name, str) for name in names) for names in lists)
            and all(len(set(names)) == len(names) for names in lists)
            and set(lists[0]) <= set(lists[1])
        ):
            wrong[key] = f"malformed fixture row {row!r}"
            continue
        required = repair_round.required_workflows(repo.github)
        if sorted(required) != sorted(row["checks"] or row["pull_request"]):
            wrong[key] = sorted(required)
        elif row["merge_queue"] and len(required) != 1:
            wrong[key] = f"merge queue with {len(required)} workflows"
    assert wrong == {}


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_owner_unreadable_record_of_another_task_refuses_and_names_its_mode(tmp_path, monkeypatch):
    """Defect class: a record another process made unreadable is skipped, or refuses without saying why."""
    from lu_machinery.review import review_lineage
    from lu_machinery.review.review_lineage import LineageRefused, review_records

    tasks = tmp_path / "tasks"
    write_task(tasks, schema_version=2, pr=42, run_nonce="nonce-1")
    write_task(tasks, task_id="b4-hidden", schema_version=2, run_nonce="nonce-2", repository="owner/other",
               research_role="evaluation", mode="read-only")
    hidden = tasks / "b4-hidden.json"
    hidden.chmod(0o400)
    assert [path.name for path, _ in review_records(tasks, repository="owner/repo")] == ["review-one.json"]
    hidden.chmod(0o000)
    try:
        with pytest.raises(LineageRefused, match=r"b4-hidden\.json unreadable: Permission denied \(mode 000: another "
                                                 r"process removed the owner's read permission\).*review run of this PR"):
            review_records(tasks, repository="owner/repo")
        with monkeypatch.context() as patch:
            patch.setattr(review_lineage.os, "geteuid", lambda: hidden.stat().st_uid + 1)
            with pytest.raises(LineageRefused, match=r"\(mode 000, owned by uid \d+, not this process\)"):
                review_records(tasks, repository="owner/repo")
        # Archived between the scan's stat and its read: the mode is the denied counterpart's own.
        read_bytes = Path.read_bytes
        archived = tasks / "archive" / hidden.name
        archived.parent.mkdir()

        def archive_first(path, *args, **kwargs):
            if path == hidden and hidden.exists():
                hidden.rename(archived)
            return read_bytes(path, *args, **kwargs)

        from lu_machinery.orchestration import task_record_store

        read_regular = task_record_store.read_regular_bytes

        def archive_first_regular(path, *args, **kwargs):
            if path == hidden and hidden.exists():
                hidden.rename(archived)
            return read_regular(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_bytes", archive_first)
            patch.setattr(task_record_store, "read_regular_bytes", archive_first_regular)
            with pytest.raises(LineageRefused, match=r"b4-hidden\.json unreadable: Permission denied \(mode 000:"):
                review_records(tasks, repository="owner/repo")
        archived.rename(hidden)
    finally:
        hidden.chmod(0o600)
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


# A sentence that must never reach a public security-review comment.
_FINDING = "The symlink operand is classified by its text, so a link escapes the root."


def _seal(tasks, comments, statuses, *, reply, profile=None):
    comments.clear()
    statuses.clear()
    task = json.loads((tasks / "review-one.json").read_text())
    if profile is None:
        task.pop("review_profile", None)
    else:
        task["review_profile"] = profile
    raw = reply.encode()
    task["result_sha256"] = hashlib.sha256(raw).hexdigest()
    (tasks / "review-one.json").write_text(json.dumps(task))
    (tasks / "review-one.result").write_bytes(raw)
    return task


def _pr_view(task, *, labels=(), issues=()):
    return {
        "number": 1,
        "headRefOid": task["worktree_base_sha"],
        "headRefName": task["worktree_branch"],
        "state": "OPEN",
        "labels": list(labels),
        "closingIssuesReferences": list(issues),
    }


def test_pr_labels_follow_the_closing_issue_repository(monkeypatch):
    """Defect class: closing issue labels come from the PR's repo or an unreadable issue gets a bare 404."""
    from lu_machinery.orchestration.fleet_repos import FleetRepo

    pr_repo = "unit/machinery"
    issue_repo = "unit/product"
    repos = {name: FleetRepo(name, f"unit/{name}", name, "test") for name in ("machinery", "product")}
    monkeypatch.setattr(recorder, "load_fleet_repos", lambda: repos)
    refs = [{"number": 403, "url": f"https://github.com/{issue_repo}/issues/403"}]
    issue_reads = []
    issue_result = (0, '{"labels":[{"name":"Security"}]}', "")

    def run(args, **_kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            assert args[3:6] == ["1", "--repo", pr_repo]
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "state": "OPEN", "labels": [], "closingIssuesReferences": refs,
            }), "")
        issue_reads.append(args)
        if args == ["gh", "issue", "view", "403", "--repo", issue_repo, "--json", "labels"]:
            return subprocess.CompletedProcess(args, *issue_result)
        return subprocess.CompletedProcess(args, 1, "", '{"error":"github_http_error","status":404}')

    monkeypatch.setattr(recorder, "request_run", run)
    pr = recorder._pr(pr_repo, "", 1)
    assert recorder._security_labeled(pr)
    assert pr["closingIssuesReferences"][0]["labels"] == [{"name": "Security"}]

    issue_result = (1, "", '{"error":"github_http_error","status":404}')
    with pytest.raises(recorder.RecordError, match=r"^LINKED_ISSUE_LABELS_UNAVAILABLE: unit/product#403 .*unreadable"):
        recorder._pr(pr_repo, "", 1)

    refs[0]["url"] = "https://github.com/other/public/issues/403"
    with pytest.raises(recorder.RecordError, match=r"^LINKED_ISSUE_LABELS_UNAVAILABLE: other/public#403 .*fleet registry"):
        recorder._pr(pr_repo, "", 1)
    assert len(issue_reads) == 2


def test_security_sensitive_verdicts_publish_the_finding_count_only(monkeypatch, low_review, tmp_path):
    """Defect class: a security-sensitive verdict is posted with its finding text.

    Profile security, a SECURITY_VERDICT, and a security label on the PR or a
    linked issue each publish the withheld form. The verdict and status stay
    the ones the same reply records today.
    """
    tasks, comments, statuses, _files, _admit = low_review
    cases = (
        ("security", f"VERDICT: CHANGES_REQUESTED\n\n{_FINDING}\n", "CHANGES_REQUESTED", (), ()),
        # The security profile is a critical review: its approval carries the sign-off line.
        ("security", attested_approval(_FINDING), "APPROVED", (), ()),
        (None, f"VERDICT: CHANGES_REQUESTED\n\nSECURITY_VERDICT: FAIL\n\n{_FINDING}\n", "CHANGES_REQUESTED", (), ()),
        (None, f"VERDICT: APPROVE\n\nSECURITY_VERDICT: PASS\n\n{_FINDING}\n", "APPROVED", (), ()),
        (None, f"VERDICT: CHANGES_REQUESTED\n\n{_FINDING}\n", "CHANGES_REQUESTED", ({"name": "security"},), ()),
        (
            None,
            f"VERDICT: APPROVE\n\n{_FINDING}\n",
            "APPROVED",
            (),
            ({"number": 403, "url": "https://github.com/unit/public/issues/403",
              "labels": [{"name": "Security"}]},),
        ),
    )
    for profile, reply, verdict, labels, issues in cases:
        task = _seal(tasks, comments, statuses, reply=reply, profile=profile)
        monkeypatch.setattr(recorder, "_pr", lambda *_a, task=task, labels=labels, issues=issues: _pr_view(
            task, labels=labels, issues=issues,
        ))
        result = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
        body = comments[-1]["body"]
        assert result["verdict"] == verdict
        assert result["comment"] == "posted" and result["status"] == "posted"
        assert f"verdict={verdict}" in body and f"VERDICT: {verdict}\n" in body
        # A sealed findings object has a known count; prose has none.
        assert f"Withheld findings: {'0' if reply.startswith('{') else 'unknown'}\n" in body
        assert "Findings are held privately.\n" in body
        assert _FINDING not in body and "SECURITY_VERDICT" not in body and "<details>" not in body
        assert statuses[-1]["description"].startswith(f"VERDICT: {verdict} ")
        assert statuses[-1]["state"] == ("success" if verdict == "APPROVED" else "failure")

    # A security profile is a critical infra sign-off. Sonnet is not one of those seats.
    task = _seal(tasks, comments, statuses, reply=f"VERDICT: APPROVE\n\n{_FINDING}\n", profile="security")
    task["model"] = "claude-sonnet-5-5"
    task["agent"] = "claude"
    (tasks / "review-one.json").write_text(json.dumps(task))
    monkeypatch.setattr(recorder, "_pr", lambda *_a, task=task: _pr_view(task))
    with pytest.raises(recorder.RecordError, match="not qualified"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []


def test_non_security_verdict_still_publishes_the_reply(monkeypatch, low_review, tmp_path):
    """Defect class: a routine verdict is published as a withheld skeleton."""
    tasks, comments, statuses, _files, _admit = low_review
    reply = f"VERDICT: CHANGES_REQUESTED\n\n{_FINDING}\n"
    task = _seal(tasks, comments, statuses, reply=reply)
    monkeypatch.setattr(recorder, "_pr", lambda *_a, task=task: _pr_view(
        task, labels=({"name": "hold"},), issues=({"number": 403, "labels": [{"name": "bug"}]},),
    ))
    result = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[-1]["body"]
    assert result["verdict"] == "CHANGES_REQUESTED" and result["status"] == "posted"
    assert _FINDING in body and "<details>" in body
    assert "Withheld findings:" not in body


def test_republish_withheld_posts_the_sealed_security_reply(monkeypatch, low_review, tmp_path):
    """Defect class: republish-withheld cannot restore a security review's sealed reply."""
    tasks, comments, statuses, _files, _admit = low_review
    reply = f"VERDICT: CHANGES_REQUESTED\n\n{_FINDING}\n"
    task = _seal(tasks, comments, statuses, reply=reply, profile="security")
    monkeypatch.setattr(recorder, "_pr", lambda *_a, task=task: _pr_view(task))
    recorded = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert "Withheld findings:" in comments[-1]["body"] and _FINDING not in comments[-1]["body"]
    republished = recorder.republish_withheld("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert recorded["verdict"] == republished["verdict"] == "CHANGES_REQUESTED"
    assert republished["comment"] == "posted"
    assert comments[0]["body"] != comments[-1]["body"]
    assert _FINDING in comments[-1]["body"] and "<details>" in comments[-1]["body"]
    assert len(statuses) == 1

    # Simulate a prior recorder's withheld approval of the same sealed P3 rejection.
    p3_reply = json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {
            "correctness": "incorrect",
            "explanation": "A residual remains.\nSECURITY_VERDICT: PASS",
            "confidence": 0.9,
        },
        "findings": [{
            "id": "F1", "title": "Residual", "body": _FINDING,
            "priority": "P3", "confidence": 0.9, "category": "correctness",
            "location": {
                "path": "lu_machinery/review/record_cf_verdict.py",
                "start_line": 1, "end_line": 1, "claim_type": "present",
            },
            "verbatim": "unit", "why_wrong": "A residual remains.", "smallest_fix": "Record it.",
            "sources": ["none"],
        }],
    })
    _seal(tasks, comments, statuses, reply=p3_reply, profile="security")
    recorded = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert recorded["verdict"] == "CHANGES_REQUESTED"
    withheld = comments[-1]["body"]
    assert "Withheld findings: 1\n" in withheld and _FINDING not in withheld
    comments[-1]["body"] = withheld.replace(
        "VERDICT: CHANGES_REQUESTED\n", "VERDICT: APPROVED\n"
    ).replace(" verdict=CHANGES_REQUESTED ", " verdict=APPROVED ")
    republished = recorder.republish_withheld("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    # The return names the reply's stated verdict. The recorded marker stays the upgraded approval.
    assert republished["comment"] == "posted" and republished["verdict"] == "CHANGES_REQUESTED"
    assert "VERDICT: APPROVED\n" in comments[0]["body"] and _FINDING not in comments[0]["body"]
    assert _FINDING in comments[-1]["body"] and "VERDICT: APPROVED\n" in comments[-1]["body"]
    assert "Residuals: F1 (P3)\n" in comments[-1]["body"] and "<details>" in comments[-1]["body"]
    assert len(statuses) == 1



def test_marker_attests_author_families():
    """Defect class: marker readers re-derive author families from their own model lists, which drift.

    GUARD-FAMILY-1125: a reader without ``grok-4.7-high`` refused a valid approval. The sweep reads the families a
    record states (AUTHORFAM-EXPAND-1125: readers ship before the recorder writes the line); old records still parse.
    """
    from tests.review.record_support import recorded_verdict

    authored = "Authored by: " + ", ".join(sorted((CURRENT_SOL, "grok-4.7-high"))) + "\n"
    line = "Author families: openai, xai\n"
    fields = dict(
        sha=SHA, task_id="review-one", started="2026-09-23T12:00:00.000001+00:00", verdict="APPROVED",
        model="claude-opus-5-5", family="anthropic", reply="VERDICT: APPROVE",
        author_models=(CURRENT_SOL, "grok-4.7-high"),
    )
    for legacy in (recorder.build_comment(**fields), recorder.build_comment(**fields, withheld_findings=0)):
        stated = legacy.replace(authored, authored + line)
        assert stated != legacy and stated.endswith(legacy.rsplit("\n", 1)[1])
        new, old = sweep.parse_marker(stated), sweep.parse_marker(legacy)
        assert new["author_families"] == ("openai", "xai") and "author_families" not in old
        assert {key: value for key, value in new.items() if key != "author_families"} == old
        for body in (stated, legacy):
            assert sweep.lookup_verdict([{**recorded_verdict(SHA, "APPROVED"), "body": body}], SHA, "fleet").state == (
                "APPROVED"
            )
        for bad in ("xai, openai", "openai, openai", "Open AI", "openai,xai", ""):
            assert sweep.parse_marker(stated.replace(line, f"Author families: {bad}\n")) is None
        assert sweep.parse_marker(stated.replace(line, line + line)) is None


def test_republish_withheld_refuses_malformed_later_trusted_marker(low_review, tmp_path):
    """A malformed later verdict must prevent republication out of order."""
    tasks, comments, statuses, _files, _admit = low_review
    _seal(tasks, comments, statuses, reply="VERDICT: CHANGES_REQUESTED\n", profile="security")
    recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    comments.append({**comments[0], "id": 2, "body": f"{sweep.MARKER_PREFIX} malformed -->"})

    with pytest.raises(recorder.RecordError, match="malformed trusted verdict marker"):
        recorder.republish_withheld("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert len(comments) == 2
    assert len(statuses) == 1


def test_republish_withheld_refuses_comment_without_timestamps(low_review, tmp_path):
    """Missing creation and update times must not pass as evidence of an unedited comment."""
    tasks, comments, statuses, _files, _admit = low_review
    _seal(tasks, comments, statuses, reply="VERDICT: CHANGES_REQUESTED\n", profile="security")
    recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    del comments[0]["created_at"], comments[0]["updated_at"]

    with pytest.raises(recorder.RecordError, match="malformed trusted verdict comment timestamps"):
        recorder.republish_withheld("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert len(comments) == 1
    assert len(statuses) == 1


def setup_record(monkeypatch, tmp_path, **kwargs):
    result = _setup_record(monkeypatch, tmp_path, **kwargs)
    # The fixture owns GitHub reads. The package also reads the PR file list
    # before choosing its learner-content seats; this is an ordinary code PR.
    monkeypatch.setattr(
        recorder.GitHubAdapter, "pr_file_rows",
        lambda *_args, **_kwargs: [{"filename": "lu_machinery/common/unit.py", "status": "modified"}],
    )
    return result


@pytest.mark.usefixtures("configured_roots", "recorder_matcher", "agent_runtime_roots")
@pytest.mark.parametrize(
    "risk,path,default_cap",
    [
        ("low", "tests/example.py", 2),
        ("medium", "lu_machinery/common/unit.py", 3),
        ("high", ".github/workflows/ci.yml", 3),
        ("critical", "lu_machinery/review/record_cf_verdict.py", 3),
    ],
)
@pytest.mark.parametrize("override", [None, 1, 5], ids=["default", "lower-override", "higher-override"])
@pytest.mark.parametrize("exhausted", [False, True], ids=["below-cap", "at-cap"])
def test_round_cap_applies_to_preflight_dispatch_and_record(
    monkeypatch, tmp_path, capsys, risk, path, default_cap, override, exhausted,
):
    """Defect classes: round-cap bypass or historical Git I/O without an approval."""
    from dataclasses import replace
    from types import SimpleNamespace

    from lu_machinery.dispatcher import checks
    from tests.review.record_support import BRANCH, REPOSITORY, facts_for, recorded_verdict

    def unexpected_git(_repo_root, args, **_kwargs):
        pytest.fail(f"round-cap history unexpectedly ran git {args[0]}")

    monkeypatch.setattr(recorder, "_facts_git", unexpected_git)
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    facts = facts_for({"google"}, changed_paths=(path,))
    facts = replace(facts, commits=tuple(replace(commit, task_id="agy/author") for commit in facts.commits))
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_a, **_k: facts)
    monkeypatch.setattr(
        recorder.GitHubAdapter, "pr_file_rows", lambda *_a: [{"filename": path, "status": "modified"}],
    )
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    policy = tmp_path / "review-budget.yaml"
    repositories = {REPOSITORY.upper(): {"review_rounds": override}} if override is not None else {}
    policy.write_text(json.dumps({"version": 1, "repositories": repositories}))
    monkeypatch.setattr(recorder, "REVIEW_BUDGET_POLICY", policy)

    cap = default_cap if override is None else override
    spent = cap if exhausted else cap - 1
    for round_number in range(1, spent + 1):
        head = f"{round_number:040x}"
        task_id = f"previous-{round_number}"
        write_task(tasks, task_id=task_id, pr=42, worktree_base_sha=head, review_risk=risk,
                   agent="claude", model="claude-opus-5-5", reply="VERDICT: REQUEST_CHANGES")
        comments.append(recorded_verdict(head, "CHANGES_REQUESTED", task_id=task_id))

    args = SimpleNamespace(pr=42, branch=BRANCH)
    cli_status = recorder.budget_check_main(["--repository", REPOSITORY, "--pr", "42"])
    preflight = json.loads(capsys.readouterr().out)
    assert cli_status == int(exhausted)
    assert preflight["admitted"] is not exhausted

    if exhausted:
        refusal = rf"REVIEW_BUDGET_EXHAUSTED: PR #42.*budget is {cap} rounds"
        assert f"budget is {cap} rounds" in preflight["refusal"]
        with pytest.raises(ValueError, match=refusal):
            checks.check_review_budget(args, {"github": REPOSITORY}, SHA)
    else:
        assert (preflight["budget"], preflight["reviewed_heads"], preflight["unrecorded_rounds"]) == (cap, spent, [])
        checks.check_review_budget(args, {"github": REPOSITORY}, SHA)

    # The review task exists by publication time. Its own head must not count toward the cap.
    # A security path or critical risk makes the approval a security sign-off.
    write_task(tasks, pr=42, review_risk=risk, review_mode="cross_family", reply=attested_approval())
    if exhausted:
        with pytest.raises(recorder.RecordError, match=refusal):
            recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
        assert calls == {"posts": 0, "statuses": 0}
    else:
        published = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
        assert published["verdict"] == "APPROVED"
        assert calls == {"posts": 1, "statuses": 1}


def captured_source_trace(tasks, *, task_id="review-one", tool="verify_word", arguments=None, output="VESUM"):
    """A provider's completed Sources event, separate from the reviewer's reply."""
    path = tasks / "logs" / f"{task_id}.stdout.log"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"event": "step_update", "step_update": {
        "state": "DONE", "step_index": 4, "step_type": "tool", "tool_name": "call_mcp_tool",
        "tool_info": {"name": "call_mcp_tool", "parameters": {
            "ServerName": "sources", "ToolName": tool, "Arguments": arguments or {"word": "example"},
        }, **({"output": output} if output is not None else {})},
    }}) + "\n")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("marker", ["trial_label", "trial_role", "trial_qualified_task"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_trial_never_becomes_review_of_record(tmp_path, monkeypatch, marker):
    from lu_machinery.review.review_lineage import review_records

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, **{marker: "trial"})
    with pytest.raises(recorder.RecordError, match="MODEL_ROLE_TRIAL_REFUSED"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and calls["posts"] == 0
    task = json.loads((tasks / "review-one.json").read_text())
    with pytest.raises(recorder.RecordError, match="MODEL_ROLE_TRIAL_REFUSED"):
        recorder.reviewer_identity(task)
    assert review_records(tasks, repository="owner/repo") == []


@pytest.fixture
def scanned_recorder(monkeypatch, tmp_path):
    """Exercise the supported publisher; fake only GitHub reads and transport."""
    from lu_machinery.opsec import prepublish as gate
    from lu_machinery.publish import github
    from tests.opsec.test_publish_workflow_inputs import real_tooling

    tooling = real_tooling()
    monkeypatch.setattr(gate, "private_tooling", lambda: tooling)
    real_json = recorder._run_json
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    bookkeeping = recorder._run_json
    sent = []

    def route(args, **kwargs):
        if isinstance(args, github.Request) and args.verb == "issue-comment-json":
            return real_json(args, **kwargs)
        return bookkeeping(args, **kwargs)

    def transport(argv, **kwargs):
        body = json.loads(Path(argv[argv.index("--input") + 1]).read_text())["body"]
        sent.append(body)
        posted = bookkeeping(github.Request("issue-comment-json", repo="owner/repo", number=42, body=body))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(posted), stderr="")

    monkeypatch.setattr(recorder, "_run_json", route)
    monkeypatch.setattr(github, "_send", transport)
    return tasks, comments, calls, sent, tooling


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("detail,rule", [
    ("2001:db8::42 SYNTHETIC-DETAIL", "2-ipv6"),
    ("credential stored SYNTHETIC-DETAIL", "5-credential-context"),
])
@pytest.mark.parametrize("verdict", ["APPROVED", "CHANGES_REQUESTED", "BLOCKED"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_real_refusal_publishes_only_skeleton(scanned_recorder, tmp_path, detail, rule, verdict):
    from lu_machinery.opsec import prepublish as gate

    tasks, comments, calls, sent, tooling = scanned_recorder
    reply = f"VERDICT: {verdict}\n{detail}\nHarmless reviewer sentence quoting `<!-- cf-verdict`.\n- A prose bullet."
    write_task(tasks, reply=reply)
    before = {suffix: (tasks / f"review-one.{suffix}").read_bytes() for suffix in ("json", "result")}
    with pytest.raises(gate.PublishBlocked, match=f"rule={rule}"):
        gate.check_texts("github.com/owner/repo", [reply], tooling=tooling, environment={})
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert body == (
        f"### Cross-family review\nhead: {SHA}\nReviewer family: openai\nVERDICT: {verdict}\n"
        f"Reviewer model: {CURRENT_SOL}\nReview score: absent\nAuthored by: gemini-3.8-flash-high\n"
        "Task id: review-one\n\nWithheld findings: unknown\n"
        "Findings are held privately.\n\n"
        f"<!-- cf-verdict v1 sha={SHA} task=review-one started=2026-09-23T12:00:00.000001+00:00 "
        f"verdict={verdict} model={CURRENT_SOL} family=openai -->"
    )
    assert sent == [body]
    assert detail not in body and "SYNTHETIC-DETAIL" not in body
    assert "Harmless reviewer sentence" not in body and "&lt;!-- cf-verdict" not in body
    assert body.count(recorder.MARKER_PREFIX) == 1
    gate.check_texts("github.com/owner/repo", [body], tooling=tooling, environment={})
    assert result["verdict"] == verdict
    assert sweep.parse_marker(body)["sha"] == SHA
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == verdict
    assert keeper._recorded_approval_for_head(comments, SHA, "fleet") == (verdict == "APPROVED")
    assert not keeper._recorded_approval_for_head(comments, "b" * 40, "fleet")
    second = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert second["comment"] == "existing" and sent == [body]
    assert calls == {"posts": 1, "statuses": 2}
    assert before == {suffix: (tasks / f"review-one.{suffix}").read_bytes() for suffix in before}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_clean_publication_bytes_unchanged(scanned_recorder, tmp_path):
    tasks, comments, calls, sent, _ = scanned_recorder
    reply = "VERDICT: APPROVE\nSCORE: 8/10 one P3 nit.\nOrdinary reviewer text.\n\n  Preserve spacing.\n"
    write_task(tasks, reply=reply)
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    expected = (
        f"### Cross-family review\nhead: {SHA}\nReviewer family: openai\nVERDICT: APPROVED\n"
        f"Reviewer model: {CURRENT_SOL}\nReview score: 8/10\nAuthored by: gemini-3.8-flash-high\n"
        "Task id: review-one\n\n"
        "<details><summary>Reviewer's reply</summary>\n\n"
        + reply + "\n\n</details>\n\n"
        f"<!-- cf-verdict v1 sha={SHA} task=review-one started=2026-09-23T12:00:00.000001+00:00 "
        f"verdict=APPROVED model={CURRENT_SOL} family=openai -->"
    )
    assert comments[0]["body"].encode() == expected.encode()
    assert sent == [expected] and calls == {"posts": 1, "statuses": 1}
    # The advisory score (C-3020) never changes the recorded verdict.
    assert result["score"] == 8 and sweep.parse_marker(expected)["verdict"] == "APPROVED"


def _ledger(tmp_path):
    path = tmp_path / "model-stats" / "publications.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("user", [None, "fleet"])
def test_review_budget_ignores_malformed_comment_author(user):
    """Defect class: a malformed REST comment author crashes review budget accounting."""
    from tests.review.record_support import recorded_verdict

    comment = {**recorded_verdict(SHA, "APPROVED"), "user": user, "author": {"login": "fleet"}}

    assert recorder._trusted_verdict_shas([comment], "fleet") == set()


@pytest.mark.usefixtures("configured_roots", "recorder_matcher", "agent_runtime_roots")
@pytest.mark.parametrize("user", [None, "fleet"])
def test_record_refuses_malformed_readback_author(monkeypatch, tmp_path, user):
    """Defect class: a malformed verdict readback author crashes before reporting unknown publication state."""
    tasks, _comments, calls = setup_record(monkeypatch, tmp_path)
    run_json = recorder._run_json

    def readback_with_malformed_user(request, **kwargs):
        response = run_json(request, **kwargs)
        if isinstance(request, recorder.Request) and request.verb == "read-comment":
            return {**response, "user": user, "author": {"login": "fleet"}}
        return response

    monkeypatch.setattr(recorder, "_run_json", readback_with_malformed_user)

    with pytest.raises(recorder.RecordError, match="comment readback mismatch; publication state unknown"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert calls == {"posts": 1, "statuses": 0}


@pytest.mark.usefixtures("configured_roots", "recorder_matcher", "agent_runtime_roots")
@pytest.mark.parametrize("user", [None, "fleet"])
def test_record_does_not_reuse_a_verdict_with_malformed_author(monkeypatch, tmp_path, user):
    """Defect class: an existing same-task marker with a malformed author crashes verdict deduplication."""
    from lu_machinery.review import capability_failure

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    comments[0].update(user=user, author={"login": "fleet"})
    # The earlier capability and budget readers have their own regressions.
    monkeypatch.setattr(capability_failure, "require_unclassified", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(recorder, "budget_admission", lambda **_kwargs: {})

    receipt = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")

    assert receipt["comment"] == "posted"
    assert calls == {"posts": 2, "statuses": 2}
    assert comments[-1]["user"] == {"login": "fleet"}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
@pytest.mark.parametrize("verdict", ["APPROVED", "CHANGES_REQUESTED"])
def test_published_verdict_stores_one_local_publication_marker(monkeypatch, tmp_path, verdict):
    """Defect class: a verdict published on the PR leaves no local marker, so the model statistics cannot tell it
    from an unpublished run (#902), or a recorder rerun stores it twice."""
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, reply=f"VERDICT: {verdict}\nOrdinary reviewer text.\n")
    first = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    second = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert (first["comment"], second["comment"]) == ("posted", "existing") and calls["posts"] == 1
    assert first["publication_marker"] == second["publication_marker"] == "stored"
    assert _ledger(tmp_path) == [{
        "schema": "review-publication.v1", "task_id": "review-one", "repository": "owner/repo", "pr": 42,
        "head": SHA, "verdict": verdict, "published_at": comments[0]["created_at"],
    }]


def _blocking_reply(priorities, explanation="The change breaks a case."):
    """A ``code-review-findings.v1`` reply with findings F1.. of these priorities (CHANGES_REQUESTED with a P0-P2)."""
    return json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "incorrect" if any(p in ("P0", "P1", "P2") for p in priorities) else "correct",
                    "explanation": explanation, "confidence": 0.9},
        "findings": [{
            "id": f"F{number}", "title": "A defect", "body": "The change breaks a case.", "priority": priority,
            "confidence": 0.9, "category": "correctness",
            "location": {"path": "scripts/example.py", "start_line": number, "end_line": number,
                         "claim_type": "present"},
            "verbatim": "value = shared_state", "why_wrong": "The state is not reset.",
            "smallest_fix": "Rebuild the state for every call.", "sources": ["none"],
        } for number, priority in enumerate(priorities, start=1)],
    })


def _outcomes(tmp_path):
    path = tmp_path / "model-stats" / "finding-outcomes.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_finding_outcome_verb_refuses_unpublished_unknown_or_nonblocking_findings(monkeypatch, tmp_path, capsys):
    """Defect class: an outcome label recorded for a finding the statistics cannot attribute to the review's own
    published blocking verdict (no publication, a publication at another head, an unknown id, a P3 finding, an
    unstructured reply), without the evidence its label needs, as free text in the ledger, as a second, different
    label of one finding, or a same-label retry refused for its new evidence."""
    import fcntl

    monkeypatch.setenv("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "author-task")
    tasks, _comments, _calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, reply=_blocking_reply(["P2", "P3"]))
    # The run census shares one observation with the reply read: it runs while the archive lock is held, so an
    # archive or restore move (which takes that lock exclusively) cannot shift a record between its two listings.
    census = recorder._run_records
    censuses = []

    def census_under_archive_lock(task_id, root):
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        censuses.append(task_id)
        return census(task_id, root)

    monkeypatch.setattr(recorder, "_run_records", census_under_archive_lock)

    def label(finding, name, evidence=None):
        return recorder.record_finding_outcome("review-one", pr_number=42, finding_id=finding, label=name,
                                               evidence=evidence, task_root=tasks)

    with pytest.raises(recorder.RecordError, match="no published CHANGES_REQUESTED verdict of review review-one"):
        label("F1", "fixed")
    assert recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")["verdict"] == (
        "CHANGES_REQUESTED")
    # A ledger line for this task on PR 43 at another head than the one it reviewed names no publication of it.
    from lu_machinery.api import model_stats_rows

    model_stats_rows.record_publications(tmp_path, [model_stats_rows.publication(
        task_id="review-one", repository="owner/repo", pr=43, head="b" * 40, verdict="CHANGES_REQUESTED",
        published_at="2026-09-23T13:00:00Z")])
    with pytest.raises(recorder.RecordError, match=r"no published CHANGES_REQUESTED verdict .* at its reviewed head"):
        recorder.record_finding_outcome("review-one", pr_number=43, finding_id="F1", label="fixed", evidence=None,
                                        task_root=tasks)
    for finding in ("F2", "F9"):
        with pytest.raises(recorder.RecordError, match="is not a P0-P2 finding of review review-one"):
            label(finding, "fixed")
    with pytest.raises(recorder.RecordError, match="label wrong needs --evidence"):
        label("F1", "wrong", "  ")
    with pytest.raises(recorder.RecordError, match="one line"):
        label("F1", "wrong", "first line\nsecond line")
    with pytest.raises(recorder.RecordError, match="unknown outcome label"):
        label("F1", "dismissed", "a word outside the vocabulary")
    assert _outcomes(tmp_path) == []
    recorded = label("F1", "wrong", "scripts/example.py:1 already resets the state")
    assert recorded["status"] == "recorded" and recorded["reviewer_model"] == CURRENT_SOL
    assert label("F1", "wrong", "scripts/example.py:1 already resets the state")["status"] == "already recorded"
    assert label("F1", "wrong", "the reset runs on every call")["status"] == "already recorded"
    with pytest.raises(recorder.RecordError, match="already labelled wrong: a finding takes one label"):
        label("F1", "fixed")
    [line] = _outcomes(tmp_path)
    assert line == {
        "schema": "review-finding-outcome.v1", "kind": "label", "task_id": "review-one", "repository": "owner/repo",
        "pr": 42, "finding_id": "F1", "head": SHA, "started_at": "2026-09-23T12:00:00.000001+00:00",
        "reviewer_model": CURRENT_SOL, "label": "wrong", "recorded_by": "author-task",
        "evidence_sha256": hashlib.sha256(b"scripts/example.py:1 already resets the state").hexdigest(),
        "recorded_at": line["recorded_at"],
    }
    # A Cursor Grok review stores its canonical catalog model id (grok-4.7), never the effort variant or display name.
    write_task(
        tasks, task_id="review-grok", reply=_blocking_reply(["P1"]), agent="cursor", model="grok-4.7-high",
        resolved_model="Grok 4.7 256K High", resolved_model_known=True, resolved_model_source="cursor-stream-json",
    )
    model_stats_rows.record_publications(tmp_path, [model_stats_rows.publication(
        task_id="review-grok", repository="owner/repo", pr=42, head=SHA, verdict="CHANGES_REQUESTED",
        published_at="2026-09-23T13:00:00Z")])
    grok_result = recorder.record_finding_outcome(
        "review-grok", pr_number=42, finding_id="F1", label="fixed", evidence=None, task_root=tasks)
    assert grok_result["status"] == "recorded" and grok_result["reviewer_model"] == "grok-4.7"
    assert _outcomes(tmp_path)[-1]["reviewer_model"] == "grok-4.7"
    # A publication posted before the task started (for example an earlier run whose archived record was purged)
    # cannot authorize a label for this run.
    write_task(tasks, task_id="review-early", reply=_blocking_reply(["P1"]),
               started_at="2026-09-23T14:00:00.000001+00:00")
    model_stats_rows.record_publications(tmp_path, [model_stats_rows.publication(
        task_id="review-early", repository="owner/repo", pr=42, head=SHA, verdict="CHANGES_REQUESTED",
        published_at="2026-09-23T13:00:00Z")])
    with pytest.raises(recorder.RecordError, match="no published CHANGES_REQUESTED verdict of review review-early"):
        recorder.record_finding_outcome("review-early", pr_number=42, finding_id="F1", label="fixed", evidence=None,
                                        task_root=tasks)
    # The command line: exit 1 and a reason on refusal, exit 0 and a JSON receipt otherwise.
    from lu_machinery.common import task_store_paths

    monkeypatch.setattr(task_store_paths, "tasks_dir", lambda *_args, **_kwargs: tasks)
    assert recorder.main(["finding-outcome", "--task-id", "review-one", "--pr", "42", "--finding", "F2",
                          "--label", "fixed"]) == 1
    assert "finding outcome refused: finding 'F2' is not a P0-P2 finding" in capsys.readouterr().out
    assert recorder.main(["finding-outcome", "--task-id", "review-one", "--pr", "42", "--finding", "F1",
                          "--label", "wrong", "--evidence", "scripts/example.py:1 already resets the state"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already recorded"
    # A reply with no structured findings takes no label.
    write_task(tasks, task_id="review-text", reply="VERDICT: CHANGES_REQUESTED\nOrdinary reviewer text.\n")
    with pytest.raises(recorder.RecordError, match="no structured findings"):
        recorder.record_finding_outcome("review-text", pr_number=42, finding_id="F1", label="fixed", evidence=None,
                                        task_root=tasks)
    # A damaged task timestamp that overflows UTC returns a typed refusal (exit 1) instead of raising OverflowError.
    for idx, overflow_ts in enumerate(("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00")):
        overflow_id = f"review-overflow-{idx}"
        write_task(tasks, task_id=overflow_id, reply=_blocking_reply(["P1"]), started_at=overflow_ts)
        assert recorder.main(["finding-outcome", "--task-id", overflow_id, "--pr", "42", "--finding", "F1",
                              "--label", "fixed"]) == 1
        assert f"finding outcome refused: review {overflow_id} has no valid start timestamp" in capsys.readouterr().out
    # The review is dispatched again under its id at the same head: the label ledger names the task id, not the run,
    # so neither run's finding takes a label any more.
    hot = tasks / "review-one.json"
    old_run = {**json.loads(hot.read_text()), "schema_version": 2, "task_id": "review-one"}
    (tasks / "review-one.20261120T120000Z.archived.json").write_text(json.dumps(old_run))
    (tasks / "review-one.20261120T120000Z.archived.result").write_bytes((tasks / "review-one.result").read_bytes())
    write_task(tasks, reply=_blocking_reply(["P2", "P2"]), started_at="2026-09-23T14:00:00.000001+00:00")
    with pytest.raises(recorder.RecordError, match="review task id review-one names 2 runs in the task store"):
        label("F2", "fixed")
    assert len(_outcomes(tmp_path)) == 2
    assert set(censuses) == {
        "review-one", "review-grok", "review-early", "review-text", "review-overflow-0", "review-overflow-1",
    }


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_record_stores_a_contest_only_against_the_previous_blocking_round(monkeypatch, tmp_path):
    """Defect class: a CONTEST line bound to another round than the PR's previous blocking review (an older round,
    the contesting review itself, a later round on a rerun, a sibling seat of the contesting review's own round, a
    round of two blocking seats taken as one, a round whose local publication marker is missing), read from another
    run's reply than the one that round published (a task id dispatched again), stored for an id that is no P0-P2
    finding of that round, a reply whose ids all miss reported as stored, or a reply that contests nothing changing
    the receipt."""
    import dataclasses

    from lu_machinery.review import capability_failure

    tasks, _comments, _calls = setup_record(monkeypatch, tmp_path)
    monkeypatch.setattr(capability_failure, "require_unclassified", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(recorder, "budget_admission", lambda **_kwargs: {})
    # Every round reviews its own head: the fake PR head moves to the head each call records.
    pr_head = {"sha": SHA}
    fake_json, fake_facts = recorder._run_json, recorder.pr_review_facts

    def moving_json(args, *, input_text=None):
        out = fake_json(args, input_text=input_text)
        if isinstance(args, list) and args[:3] == ["gh", "pr", "view"]:
            return {**out, "headRefOid": pr_head["sha"]}
        if isinstance(args, list) and args[:3] == ["gh", "pr", "list"]:
            return [{**item, "headRefOid": pr_head["sha"]} for item in out]
        return out

    monkeypatch.setattr(recorder, "_run_json", moving_json)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *args, **kwargs: dataclasses.replace(
        fake_facts(*args, **kwargs), head_sha=pr_head["sha"]))

    def rerun(task_id, head):
        pr_head["sha"] = head
        return recorder.record(task_id, pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")

    def publish(task_id, reply, head, **updates):
        write_task(tasks, task_id=task_id, reply=reply, worktree_base_sha=head, **updates)
        return rerun(task_id, head)

    first = publish("review-one", _blocking_reply(["P1", "P2", "P3"]), "1" * 40)
    assert "outcome_contests" not in first
    # Every fake comment has the same GitHub time: the PR's comment order is the round order.
    second = publish("review-two", _blocking_reply(
        ["P1"], "Two labels do not hold.\nCONTEST F2: the reset is still missing\n  CONTEST F3: P3 is no label\n"
                "CONTEST F9: no such finding\nA prose CONTEST F1: mid-line is no contest"), "2" * 40)
    assert second["outcome_contests"] == "stored against review-one: F2; unmatched: F3, F9"
    # Round 2's local marker is lost (a failed store): the PR's own comments still say round 2 came before round 3.
    ledger = tmp_path / "model-stats" / "publications.jsonl"
    ledger.write_text("".join(line + "\n" for line in ledger.read_text().splitlines() if '"review-two"' not in line))
    third = publish("review-three", _blocking_reply(["P1"], "CONTEST F1: still open\nCONTEST F2: still open"), "3" * 40)
    assert third["outcome_contests"] == "stored against review-two: F1; unmatched: F2"
    assert [(line["task_id"], line["finding_id"], line["contested_by"]) for line in _outcomes(tmp_path)] == [
        ("review-one", "F2", "review-two"), ("review-two", "F1", "review-three")]
    # A rerun stores nothing twice, and a rerun of round 2 after round 3 still binds round 1.
    assert rerun("review-three", "3" * 40)["outcome_contests"] == "stored against review-two: F1; unmatched: F2"
    assert rerun("review-two", "2" * 40)["outcome_contests"] == "stored against review-one: F2; unmatched: F3, F9"
    assert len(_outcomes(tmp_path)) == 2
    # A reply whose contested ids are all no P0-P2 finding of the previous round stores nothing and says so.
    assert publish("review-four", _blocking_reply(["P1"], "CONTEST F7: no such finding"), "4" * 40)[
        "outcome_contests"] == "not stored: unmatched: F7"
    # Round 4's task id is dispatched again at the same head: its task record is now another run than the one its
    # comment published, so a contest of round 4 is unresolved and writes nothing.
    write_task(tasks, task_id="review-four", reply=_blocking_reply(["P1", "P1"]), worktree_base_sha="4" * 40,
               started_at="2026-09-23T14:00:00.000001+00:00")
    assert publish("review-five", _blocking_reply(["P1"], "CONTEST F1: still open"), "5" * 40)[
        "outcome_contests"] == f"not stored: unresolved: the task record of review-four is not the run published at {'4' * 12}"
    assert len(_outcomes(tmp_path)) == 2
    # Two seats review round 6. The first publishes its rejection before the second records a contest: the sibling
    # is the same round, so the second seat's contest still binds round 5.
    publish("review-six-a", _blocking_reply(["P1"]), "6" * 40)
    assert publish("review-six-b", _blocking_reply(["P1"], "CONTEST F1: still open"), "6" * 40)[
        "outcome_contests"] == "stored against review-five: F1"
    # Round 7 contests round 6, which has two blocking seats: F1 names a finding of each, so nothing is stored.
    assert publish("review-seven", _blocking_reply(["P1"], "CONTEST F1: still open"), "7" * 40)[
        "outcome_contests"] == f"not stored: unresolved: the previous round at {'6' * 12} has 2 blocking review(s) of another task"
    # Round 8 approves; round 9 contests F1: the previous round holds no blocking review, so the contest never falls
    # back past it to round 7.
    assert publish("review-eight", _blocking_reply([]), "8" * 40)["verdict"] == "APPROVED"
    assert publish("review-nine", _blocking_reply(["P1"], "CONTEST F1: still open"), "9" * 40)[
        "outcome_contests"] == f"not stored: unresolved: the previous round at {'8' * 12} has 0 blocking review(s) of another task"
    # Round 9's task id is dispatched again for round 10 and contests round 9's F1: that is its own task, so it is
    # unresolved, never an older round.
    assert publish("review-nine", _blocking_reply(["P1"], "CONTEST F1: still open"), "a" * 40,
                   started_at="2026-09-23T15:00:00.000001+00:00")["outcome_contests"] == (
        f"not stored: unresolved: the previous round at {'9' * 12} has 0 blocking review(s) of another task; this task "
        "id blocked it too")
    assert [(line["task_id"], line["finding_id"], line["contested_by"], line["head"], line["started_at"])
            for line in _outcomes(tmp_path)][2:] == [
        ("review-five", "F1", "review-six-b", "5" * 40, "2026-09-23T12:00:00.000001+00:00")]
    # PR1553-R1-F1: label F1 of review-ten run 1, then reuse task id review-ten on a newer head/start on the same PR
    # and contest F1 of run 2 from review-eleven. The contest carries run 2's head/start and never suppresses run 1's
    # label.
    run1_start = "2026-09-23T12:00:00.000001+00:00"
    run2_start = "2026-09-23T12:30:00.000001+00:00"
    assert publish("review-ten", _blocking_reply(["P1"]), "b" * 40, started_at=run1_start)["verdict"] == "CHANGES_REQUESTED"
    assert recorder.record_finding_outcome(
        "review-ten", pr_number=42, finding_id="F1", label="fixed", evidence=None, task_root=tasks)["status"] == "recorded"
    hot_ten = tasks / "review-ten.json"
    (tasks / "review-ten.20260923T121500Z.archived.json").write_text(
        json.dumps({**json.loads(hot_ten.read_text()), "schema_version": 2, "task_id": "review-ten"}))
    (tasks / "review-ten.20260923T121500Z.archived.result").write_bytes((tasks / "review-ten.result").read_bytes())
    assert publish("review-ten", _blocking_reply(["P1"]), "c" * 40, started_at=run2_start)["verdict"] == "CHANGES_REQUESTED"
    assert publish("review-eleven", _blocking_reply(["P1"], "CONTEST F1: run 2 finding still open"), "d" * 40)[
        "outcome_contests"] == "stored against review-ten: F1"
    from lu_machinery.api import model_stats_rows

    labels, contests, _ = model_stats_rows.read_finding_outcomes(tmp_path)
    repo_id = model_stats_rows.repository_identity("owner/repo")
    run1_key = ("review-ten", repo_id, 42, "F1", "b" * 40, model_stats_rows.timestamp(run1_start))
    run2_key = ("review-ten", repo_id, 42, "F1", "c" * 40, model_stats_rows.timestamp(run2_start))
    assert run1_key in labels and run1_key not in contests and run2_key in contests


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_unknown_outcome_label_holds_its_finding_key(monkeypatch, tmp_path):
    """Defect class: a well-formed label line whose label this release does not know (a newer release wrote it, then
    the host rolled back) read as damaged, so the writer accepts a second, different label for its finding and the
    statistics attribute that second label to the reviewer. Controls: a known label is still attributed, a damaged
    label is still malformed, and no writer path writes an unknown label."""
    from lu_machinery.api import model_stats_rows

    monkeypatch.setenv("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "author-task")
    tasks, _comments, _calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, reply=_blocking_reply(["P1", "P1", "P1"]))
    assert recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")["verdict"] == (
        "CHANGES_REQUESTED")
    started = "2026-09-23T12:00:00.000001+00:00"
    newer = {
        "schema": "review-finding-outcome.v1", "kind": "label", "task_id": "review-one", "repository": "owner/repo",
        "pr": 42, "finding_id": "F1", "head": SHA, "started_at": started, "reviewer_model": CURRENT_SOL,
        "label": "future_label", "evidence_sha256": hashlib.sha256(b"a newer release's evidence").hexdigest(),
        "recorded_by": "author-task", "recorded_at": "2026-09-23T13:30:00+00:00",
    }
    damaged = {**newer, "finding_id": "F3", "label": "Not A Label"}
    ledger = tmp_path / "model-stats" / "finding-outcomes.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(newer) + "\n" + json.dumps(damaged) + "\n")

    with pytest.raises(recorder.RecordError, match="already labelled future_label: a finding takes one label"):
        recorder.record_finding_outcome("review-one", pr_number=42, finding_id="F1", label="fixed", evidence=None,
                                        task_root=tasks)
    assert recorder.record_finding_outcome("review-one", pr_number=42, finding_id="F2", label="fixed", evidence=None,
                                           task_root=tasks)["status"] == "recorded"
    with pytest.raises(ValueError, match="wrong shape"):
        model_stats_rows.record_finding_outcomes(tmp_path, [{**newer, "finding_id": "F4"}])
    assert [line["finding_id"] for line in _outcomes(tmp_path)] == ["F1", "F3", "F2"]

    labels, _contests, counts = model_stats_rows.read_finding_outcomes(tmp_path)
    f2 = ("review-one", model_stats_rows.repository_identity("owner/repo"), 42, "F2", SHA,
          model_stats_rows.timestamp(started))
    assert list(labels) == [f2] and labels[f2][:2] == (CURRENT_SOL, "fixed")
    assert counts == {"lines": 2, "unknown_labels": 1, "malformed_lines": 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_unwritable_publication_marker_never_undoes_the_publication(monkeypatch, tmp_path):
    """Defect class: a marker write failure hides the posted verdict, retries the post, or passes silently; the
    receipt must name the failure and a recovery that still works once the PR has moved on (a rerun of record
    refuses then), and that recovery must store the marker without posting again."""
    from lu_machinery.api import model_stats_rows

    tasks, _comments, calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, reply="VERDICT: APPROVE\nOrdinary reviewer text.\n")
    real = model_stats_rows.record_publications

    def busy(*_args, **_kwargs):
        raise TimeoutError("the model-stats rows lock is busy")

    monkeypatch.setattr(model_stats_rows, "record_publications", busy)
    first = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert first["comment"] == "posted" and first["status"] == "posted"
    assert first["publication_marker"].startswith("failed: TimeoutError; store it with: ")
    assert first["publication_marker"].endswith(" import-publications --repository owner/repo --pr 42")
    assert _ledger(tmp_path) == []
    monkeypatch.setattr(model_stats_rows, "record_publications", real)
    # The PR head has moved on, so record itself would refuse; the one-PR import reads the posted comment.
    monkeypatch.setattr(recorder, "_pr", lambda *_a, **_k: pytest.fail("the import never reads the PR head"))
    counts = recorder.import_publications(days=1, dry_run=False, task_root=tasks, only=("owner/repo", 42))
    assert (counts["prs_read"], counts["added"], calls["posts"]) == (1, 1, 1)
    assert [entry["verdict"] for entry in _ledger(tmp_path)] == ["APPROVED"]


@pytest.mark.usefixtures("configured_roots")
def test_import_publications_keeps_only_trusted_unedited_verdicts_of_record(tmp_path, monkeypatch):
    """The history import preserves trusted verdicts, skips voids and second reviews, and tolerates a rate limit."""
    from lu_machinery.common.github_client import GitHubRateLimited
    from datetime import UTC, datetime

    from lu_machinery.api import model_stats_rows
    from lu_machinery.orchestration.fleet_repos import FleetRepo
    from tests.review.record_support import REPOSITORY, recorded_verdict

    now = datetime(2026, 9, 30, tzinfo=UTC)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    base = {"schema": model_stats_rows.ROW_SCHEMA, "harness": "codex", "model": "gpt-6.2-sol", "effort": "high",
            "role": "review", "class": "code/medium", "status": "done", "never_started_reason": None,
            "duration_s": None, "tokens": {"fresh_input": 0, "cached_input": 0, "output": 0}, "allowance": None,
            "review": None, "repo": "unit", "data_class": None, "pinned_head": None, "author_model": None,
            "landing": None, "source_mtime_ns": 1, "started_at": None}
    for task_id, pr, finished in (("fresh-review", 42, "2026-09-29T00:00:00+00:00"),
                                  ("limited-review", 43, "2026-09-29T00:00:00+00:00"),
                                  ("old-review", 7, "2026-08-01T00:00:00+00:00"),
                                  ("write-task", 8, "2026-09-29T00:00:00+00:00")):
        model_stats_rows.append_row(tmp_path, {**base, "task_id": task_id, "pr": pr, "finished_at": finished,
                                               **({"role": "write", "class": "unclassified"}
                                                  if task_id == "write-task" else {})})
    other_head = "c" * 40
    written = recorded_verdict(SHA, "CHANGES_REQUESTED", task_id="p3-review")
    started = {"sha": SHA, "started": "2026-09-23T11:00:00.000001+00:00", "model": "claude-opus-5-5",
               "family": "anthropic"}
    comments = [
        recorded_verdict(SHA, "CHANGES_REQUESTED", task_id="fresh-review"),
        recorded_verdict(other_head, "APPROVED", task_id="later-review"),
        {**recorded_verdict(SHA, "APPROVED", task_id="stranger-review"), "user": {"login": "someone"}},
        {**recorded_verdict(SHA, "APPROVED", task_id="outside-review"), "author_association": "NONE"},
        recorded_verdict(SHA, "APPROVED", task_id="edited-review", edited=True),
        {**recorded_verdict(SHA, "APPROVED", task_id="nulluser-review"), "user": None},
        {**recorded_verdict(SHA, "APPROVED", task_id="listassoc-review"), "author_association": ["MEMBER"]},
        {**written, "body": recorder.build_comment(task_id="p3-review", verdict="CHANGES_REQUESTED",
                                                   reply=_all_p3_object(), **started)},
        {**written, "body": recorder.build_comment(
            task_id="void-review", verdict="APPROVED", reply="VERDICT: APPROVED", review_profile="ukrainian",
            sources_mcp_call_count=3, denominator=["i1"], coverage=[], void="quote-mismatch", **started)},
        {**written, "body": recorder.build_comment(
            task_id="second-review", verdict="APPROVED", reply="VERDICT: APPROVED", second_review=True,
            **{**started, "model": "gemini-4-carbon-medium", "family": "google"})},
        {"body": "an ordinary comment", "user": {"login": "fleet"}, "author_association": "MEMBER"},
    ]
    read = []

    class Adapter:
        def identity(self):
            return "fleet"

        def comments(self, repository, number):
            read.append((repository, number))
            if number == 43:
                raise GitHubRateLimited(1_900_000_000, "secondary")
            return comments

    catalog = {"unit": FleetRepo("unit", REPOSITORY, "unit", "test")}
    monkeypatch.setattr(recorder, "load_fleet_repos", lambda: catalog)
    dry = recorder.import_publications(days=30, dry_run=True, task_root=tasks, now=now, adapter=Adapter())
    assert _ledger(tmp_path) == [] and read == [(REPOSITORY, 42), (REPOSITORY, 43)]
    assert dry == {"prs": 2, "prs_read": 1, "prs_unreadable": 0, "rate_limited": 1, "markers": 3,
                   "markers_untrusted": 5, "markers_skipped": 2, "rows_unregistered_repo": 0, "would_add": 3}
    # What was read before the rate limit is stored.
    done = recorder.import_publications(days=30, dry_run=False, task_root=tasks, now=now, adapter=Adapter())
    assert (done["added"], done["rate_limited"]) == (3, 1)
    assert [(e["task_id"], e["pr"], e["head"], e["verdict"], e["published_at"]) for e in _ledger(tmp_path)] == [
        ("fresh-review", 42, SHA, "CHANGES_REQUESTED", "2026-09-23T11:30:00Z"),
        ("later-review", 42, other_head, "APPROVED", "2026-09-23T11:30:00Z"),
        ("p3-review", 42, SHA, "CHANGES_REQUESTED", "2026-09-23T11:30:00Z"),
    ]
    again = recorder.import_publications(days=30, dry_run=False, task_root=tasks, now=now, adapter=Adapter())
    assert again["added"] == 0 and len(_ledger(tmp_path)) == 3


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_skeleton_scanner_rejection_has_no_write(scanned_recorder, tmp_path, monkeypatch, synthetic_opsec):
    from lu_machinery.opsec import prepublish as gate
    from tests.opsec_fixtures import synthetic_rules

    tasks, comments, calls, sent, _ = scanned_recorder
    rules = synthetic_rules(rule="5-credential-context", level=5, pattern="credential stored")
    rules["1"] = {"patterns": [{"id": "synthetic-skeleton-refusal", "regex": "Findings are held privately"}]}
    rules["3"] = {"patterns": [{"id": "3-absolute-path", "regex": r"(?!)"}]}
    (synthetic_opsec / "rules.json").write_text(json.dumps(rules))
    monkeypatch.setattr(gate, "private_tooling", lambda: synthetic_opsec)
    write_task(tasks, reply="VERDICT: APPROVE\ncredential stored SYNTHETIC-DETAIL")
    with pytest.raises(recorder.RecordError, match="synthetic-skeleton-refusal"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == sent == [] and calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("failure", ["scanner_unavailable", "lookup_failed"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_unrelated_failure_never_uses_skeleton(scanned_recorder, tmp_path, monkeypatch, failure):
    from lu_machinery.opsec import prepublish as gate

    tasks, comments, calls, sent, _ = scanned_recorder
    scans = []

    def refuse(destination, texts, **kwargs):
        scans.extend(texts)
        if failure == "scanner_unavailable":
            raise gate.PublishBlocked("OPSEC: checker unavailable; write refused.")
        raise recorder.RecordError("GitHub lookup or publication unavailable")

    monkeypatch.setattr(recorder, "check_texts", refuse)
    with pytest.raises(recorder.RecordError, match="unavailable"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert len(scans) == 1 and "Withheld findings:" not in scans[0]
    assert comments == sent == [] and calls == {"posts": 0, "statuses": 0}


def structured_review(count):
    return {
        "schema_version": "code-review-findings.v1",
        "overall": {
            "correctness": "incorrect" if count else "correct",
            "explanation": "credential stored SYNTHETIC-DETAIL",
            "confidence": 0.9,
        },
        "findings": [{
            "id": f"F{number}", "title": "Synthetic finding", "body": "Synthetic finding body.",
            "priority": "P2", "confidence": 0.9, "category": "correctness",
            "location": {"path": "scripts/example.py", "start_line": number, "end_line": number,
                         "claim_type": "present"},
            "verbatim": "value = shared_state", "why_wrong": "The state is not reset.",
            "smallest_fix": "Rebuild the state for every call.", "sources": ["none"],
        } for number in range(1, count + 1)],
    }


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("count", [0, 1, 3])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_validated_structured_count_in_skeleton(scanned_recorder, tmp_path, count):
    tasks, comments, calls, sent, _ = scanned_recorder
    reply = json.dumps(structured_review(count))
    write_task(tasks, reply=reply)
    before = (tasks / "review-one.result").read_bytes()
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    expected = "CHANGES_REQUESTED" if count else "APPROVED"
    assert result["verdict"] == sweep.lookup_verdict(comments, SHA, "fleet").state == expected
    assert f"Withheld findings: {count}\n" in comments[0]["body"]
    assert "SYNTHETIC-DETAIL" not in comments[0]["body"]
    assert "Synthetic finding" not in comments[0]["body"]
    assert (tasks / "review-one.result").read_bytes() == before
    assert sent == [comments[0]["body"]] and calls == {"posts": 1, "statuses": 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_approve_reply_with_one_p3_finding_records_approve_with_score(monkeypatch, tmp_path):
    """A P3 finding is a follow-up, so the reply's APPROVE and its score are recorded (#12022)."""
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    payload = structured_review(1)
    payload["overall"].update(
        correctness="correct", explanation="VERDICT: APPROVE. SCORE: 9/10.\n\nOne non-blocking P3 follow-up."
    )
    payload["findings"][0]["priority"] = "P3"
    write_task(tasks, reply=json.dumps(payload))
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert calls == {"posts": 1, "statuses": 1}
    assert result["verdict"] == sweep.lookup_verdict(comments, SHA, "fleet").state == "APPROVED"
    assert result["score"] == 9 and "VERDICT: APPROVED\n" in body and "Review score: 9/10\n" in body
    assert result["residuals"] == ["F1"] and "Residuals: F1 (P3)\n" in body


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_changes_requested_with_only_p3_findings_records_rejection(monkeypatch, tmp_path):
    """An all-P3 REQUEST_CHANGES records a rejection and leaves the reply intact."""
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    payload = structured_review(2)
    payload["overall"].update(
        correctness="incorrect",
        explanation="VERDICT: REQUEST_CHANGES. SCORE: 6/10. The only open findings are P3 regressions.",
    )
    for finding in payload["findings"]:
        finding["priority"] = "P3"
    reply = json.dumps(payload)
    write_task(tasks, reply=reply)
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields))
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert reply in body and "Residuals:" not in body
    assert result["verdict"] == "CHANGES_REQUESTED" and "residuals" not in result
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "CHANGES_REQUESTED"
    assert statuses == [{
        "repository": "owner/repo",
        "head_sha": SHA,
        "state": "failure",
        "context": "fleet/cross-family-review",
        "description": "VERDICT: CHANGES_REQUESTED openai review-one",
    }]
    assert calls["posts"] == 1


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("blocking", ["P0", "P1", "P2"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_pure_findings_object_with_a_blocking_entry_records_rejection(monkeypatch, tmp_path, blocking):
    """A rejection with findings of different priorities stays a rejection."""
    tasks, comments, _ = setup_record(monkeypatch, tmp_path)
    payload = structured_review(2)
    payload["overall"]["explanation"] = "VERDICT: REQUEST_CHANGES. One finding blocks."
    payload["findings"][0]["priority"] = "P3"
    payload["findings"][1]["priority"] = blocking
    write_task(tasks, reply=json.dumps(payload))
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields))
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert result["verdict"] == "CHANGES_REQUESTED" and "residuals" not in result
    assert "Residuals:" not in body
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "CHANGES_REQUESTED"
    assert statuses[0]["state"] == "failure"


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_security_verdict_fail_keeps_changes_requested(scanned_recorder, tmp_path, monkeypatch):
    """SECURITY_VERDICT FAIL with only P3 findings stays CHANGES_REQUESTED and status failure."""
    tasks, comments, _, _, _ = scanned_recorder
    reply = (
        "VERDICT: REQUEST_CHANGES\n"
        "SCORE: 4/10 the security verdict stands.\n"
        "SECURITY_VERDICT FAIL\n"
        "F1 [P3] scripts/example.py:10 the check is skipped. Impact: a refused input is accepted. "
        "Required behaviour: the refusal stays.\n"
    )
    write_task(tasks, reply=reply)
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields))
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert result["verdict"] == "CHANGES_REQUESTED" and "residuals" not in result
    assert "Residuals:" not in body
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "CHANGES_REQUESTED"
    assert statuses[0]["state"] == "failure" and statuses[0]["context"] == "fleet/cross-family-review"
    assert statuses[0]["description"] == "VERDICT: CHANGES_REQUESTED openai review-one"


def security_finding(priority="P2", finding_id="F1"):
    return {
        "id": finding_id, "title": "Link escape", "body": _FINDING, "priority": priority, "confidence": 0.9,
        "category": "security",
        "location": {"path": "scripts/example.py", "start_line": 1, "end_line": 1, "claim_type": "present"},
        "verbatim": "value = shared_state", "why_wrong": "The link is classified by its text.",
        "smallest_fix": "Resolve the link first.", "sources": ["none"],
    }


def security_reply(correctness, priority="P2"):
    return json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": correctness, "explanation": "One security finding.", "confidence": 0.9},
        "findings": [security_finding(priority)],
    })


def _recording_statuses(monkeypatch):
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields))
    return statuses


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("priority", ["P2", "P3"])
@pytest.mark.parametrize("lead", ["", "\n    "], ids=["bare", "indented"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_correct_reply_with_security_finding_records_rejection_withheld(monkeypatch, tmp_path, priority, lead):
    """Defect class: security finding approved, and published on an unlabeled PR.

    A bare canonical ``correct`` reply with a security P2 or P3 records CHANGES_REQUESTED with a failure
    status; the comment keeps only the count and the private ledger keeps the true priority. Leading
    whitespace that Markdown reads as an indented code block does not hide the finding.
    """
    from lu_machinery.api import model_stats_rows

    tasks, comments, _calls = setup_record(monkeypatch, tmp_path)
    statuses = _recording_statuses(monkeypatch)
    reply = lead + security_reply("correct", priority)
    write_task(tasks, reply=reply)
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert result["verdict"] == sweep.lookup_verdict(comments, SHA, "fleet").state == "CHANGES_REQUESTED"
    assert statuses[0]["state"] == "failure" and "residuals" not in result
    assert "Withheld findings: 1\n" in body and "Findings are held privately.\n" in body
    assert _FINDING not in body and "<details>" not in body and "(P3)" not in body
    summary = model_stats_rows.review_summary((tasks / "review-one.result").read_bytes())
    assert summary["verdict"] == "incorrect" and summary["priorities"] == {priority: 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("reply", [
    approve_envelope(attested_payload(findings=(security_finding("P3"),))),
    approve_envelope(attested_payload(attestation="SECURITY_VERDICT: FAIL")),
    attested_approval(attestation="SECURITY_VERDICT: FAIL"),
    "VERDICT: APPROVE\n\nSECURITY_VERDICT: FAIL\n",
], ids=["envelope-with-security-finding", "envelope-security-fail", "sealed-security-fail", "prose-security-fail"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_approve_header_with_open_security_signal_is_refused(monkeypatch, tmp_path, reply):
    """Defect class: security finding approved under an explicit APPROVE header; nothing is posted."""
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, reply=reply)
    with pytest.raises(recorder.RecordError, match=r"verdict line|SECURITY_APPROVAL_REFUSED"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("source", ["task-record", "withheld-comment", "unpublished-run"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_inherited_security_finding_omitted_from_the_latest_reply_blocks(monkeypatch, tmp_path, source):
    """Defect class: security finding approved after a later round silently omits it.

    An earlier round's security finding binds every later APPROVE until a critical seat signs off. A prior
    reply that cannot be read (record missing, comment withheld) binds the same way, and so does a finished
    review run of the PR whose verdict was never published (no marker).
    """
    from tests.review.record_support import OTHER, recorded_verdict

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    statuses = _recording_statuses(monkeypatch)
    prior = recorded_verdict(OTHER, "CHANGES_REQUESTED", task_id="review-zero")
    if source in {"task-record", "unpublished-run"}:
        write_task(tasks, task_id="review-zero", worktree_base_sha=OTHER, reply=security_reply("incorrect", "P3"),
                   started_at="2026-09-23T11:00:00.000001+00:00", review_mode="cross_family")
    else:
        marker = prior["body"].rsplit("\n", 1)[1]
        prior["body"] = recorder.build_comment(
            sha=OTHER, task_id="review-zero", started="2026-09-23T11:00:00.000001+00:00",
            verdict="CHANGES_REQUESTED", model="claude-opus-5-5", family="anthropic", reply="",
            withheld_findings=1,
        )
        assert prior["body"].endswith(marker)
    if source != "unpublished-run":
        comments.append(prior)
    published = len(comments)
    clean = json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": "The fix holds.", "confidence": 0.9},
        "findings": [],
    })
    write_task(tasks, reply=clean)
    with pytest.raises(recorder.RecordError, match=r"SECURITY_APPROVAL_REFUSED: another review of this PR raised "
                                                   r"a security finding \(review-zero"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert len(comments) == published and calls == {"posts": 0, "statuses": 0}

    write_task(tasks, reply=attested_approval("The fix holds."))
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED" and statuses[-1]["state"] == "success"


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("critical", [
    {"path": "lu_machinery/review/record_cf_verdict.py"},
    {"path": "lu_machinery/common/unit.py", "review_risk": "critical"},
    {"path": "lu_machinery/common/unit.py", "review_profile": "security"},
], ids=["security-path", "critical-risk", "security-profile"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_critical_approval_needs_one_security_attestation_line(monkeypatch, tmp_path, critical):
    """Defect class: security finding approved on a critical review without the reviewer's security sign-off.

    APPROVE needs a reply that is one JSON object whose ``overall.explanation`` opens with exactly the line
    ``SECURITY_VERDICT: PASS``, the reply's only security verdict, decoded strings included. The proof runs
    the reviewer's raw output through the worker's schema-bound sealing (``structured_result``), which is
    the only form a critical seat's reply reaches the recorder in.
    """
    from lu_machinery.agent_runtime.adapters._output_schema import json_value, structured_result
    from lu_machinery.review.review_contract import schema_path
    from tests.review.record_support import facts_for

    schema = json.loads(schema_path(profile="infra").read_text(encoding="utf-8"))

    def sealed(raw):
        result = structured_result(json_value(raw), schema, returncode=0)
        return result.response if result.ok else None

    path = critical.pop("path")
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    statuses = _recording_statuses(monkeypatch)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_a, **_k: facts_for({"google"}, changed_paths=(path,)))
    monkeypatch.setattr(recorder.GitHubAdapter, "pr_file_rows", lambda *_a: [{"filename": path, "status": "modified"}])
    # The plan's first placement, a line after SCORE outside the object, never survives the sealing.
    old_placement = approve_envelope(attested_payload(attestation=None)).replace(
        "SCORE: 9/10\n", "SCORE: 9/10\nSECURITY_VERDICT: PASS\n")
    assert sealed(old_placement) is None and sealed(approve_envelope(attested_payload())) is None
    refused = {
        "no-sign-off": attested_approval(attestation=None),
        "second-line": attested_approval("SECURITY_VERDICT: PASS", attestation="Clean."),
        "inline": attested_approval("Clean. SECURITY_VERDICT: PASS", attestation=None),
        "trailing-space": attested_approval(attestation="SECURITY_VERDICT: PASS "),
        "lowercase": attested_approval(attestation="security_verdict: pass"),
        "two-declarations": attested_approval("Also SECURITY_VERDICT: PASS."),
        # The second declaration is a JSON escape: only its decoded string spells the key.
        "escaped-duplicate": attested_approval("Clean. SECURITY_VERDICT: PASS").replace(
            "Clean. SECURITY_VERDICT", "Clean. SECURITY\\u005fVERDICT"),
        "in-a-finding": attested_approval(findings=({**security_finding("P3"), "category": "correctness",
                                                    "body": "SECURITY_VERDICT: PASS"},)),
        "qualified-pass": attested_approval(attestation="SECURITY_VERDICT: PASS (pending F1)"),
        "envelope": approve_envelope(attested_payload()),
        "old-placement": old_placement,
    }
    for name, reply in refused.items():
        if name not in {"envelope", "old-placement"}:
            assert sealed(reply) is not None, name
        write_task(tasks, reply=reply, **critical)
        with pytest.raises(recorder.RecordError, match="SECURITY_APPROVAL_REFUSED"):
            recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
        assert comments == [] and calls == {"posts": 0, "statuses": 0}, name

    # The sign-off line has no closing punctuation: an unfinished-review admission on the next line, or after
    # any unpunctuated line, is still read, whether the object is sealed or indented as a Markdown code block.
    from lu_machinery.review.review_contract import ContractError, canonical_receipt_payload

    for unfinished in (
        attested_approval("I could not finish the review."),
        attested_approval("Summary\nI could not finish the review."),
        "\n    " + attested_approval("I could not finish the review."),
        "```json\n" + attested_approval("I could not finish the review.") + "\n```\n",
    ):
        # A whole fence reaches the recorder only from a route the worker does not seal.
        assert unfinished.startswith("```") or sealed(unfinished) is not None
        with pytest.raises(ContractError, match="review_declares_incomplete"):
            canonical_receipt_payload(unfinished)
        write_task(tasks, reply=unfinished, **critical)
        with pytest.raises(recorder.RecordError, match="inconclusive: reviewer declares an incomplete review"):
            recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
        assert comments == [] and calls == {"posts": 0, "statuses": 0}

    # A reviewer's pretty-printed raw output, sealed by the worker, signs off.
    reply = sealed(json.dumps(attested_payload(), indent=2))
    assert reply == attested_approval()
    write_task(tasks, reply=reply, **critical)
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED" and statuses[-1]["state"] == "success"

    # Only a critical_review seat signs off.
    with pytest.raises(recorder.RecordError, match="needs a critical_review seat"):
        recorder._require_security_approval(attested_approval(), critical=True, critical_seat=False, inherited=())
    assert recorder._critical_review_seat({"agent": "codex"}, CURRENT_SOL)
    assert not recorder._critical_review_seat({"agent": "claude"}, "claude-sonnet-5-5")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("correctness,verdict", [("incorrect", "CHANGES_REQUESTED"), ("uncertain", "CHANGES_REQUESTED")])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_incorrect_or_uncertain_security_reply_never_approves(monkeypatch, tmp_path, correctness, verdict):
    """Defect class: security finding approved; a rejection or uncertainty is never promoted to approval."""
    tasks, comments, _calls = setup_record(monkeypatch, tmp_path)
    statuses = _recording_statuses(monkeypatch)
    write_task(tasks, reply=security_reply(correctness, "P3"))
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == sweep.lookup_verdict(comments, SHA, "fleet").state == verdict
    assert statuses[0]["state"] == "failure"


def _all_p3_object():
    payload = structured_review(1)
    payload["overall"]["explanation"] = "One P3 residual."
    payload["findings"][0]["priority"] = "P3"
    return json.dumps(payload)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize(("deep", "reason"), [
    (False, "contradicting a verdict line"),
    (True, "invalid findings block beside a verdict line: top-level blocks cannot be decided"),
], ids=["request-changes-envelope", "unterminated-deep-array-before-invalid-envelope"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_approve_line_beside_top_level_request_changes_envelope_is_refused(deep, reason):
    """An outer APPROVE never outranks a top-level findings block that requests changes, or one that cannot be read (#12040)."""
    payload = structured_review(1)
    payload["findings"][0]["priority"] = "P1"
    block = "[" * 11000 + '0\n\n{"findings":"bad"}' if deep else json.dumps(payload, indent=2)
    with pytest.raises(recorder.RecordError, match=reason):
        recorder.normalize_verdict("VERDICT: APPROVE\n\n" + block)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("update", [
    {"findings": "not a list"}, {"findings": [None]}, {"verdict": "APPROVED"},
    {"verdict": "INVALID"}, {"overall": {}},
    {"overall": {"correctness": "correct", "explanation": "", "confidence": 0.9}, "findings": []},
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_invalid_structured_verdict_or_count_refuses(scanned_recorder, tmp_path, update):
    tasks, comments, calls, sent, _ = scanned_recorder
    payload = structured_review(1)
    payload.update(update)
    write_task(tasks, reply=json.dumps(payload))
    with pytest.raises(recorder.RecordError, match="structured review verdict or findings invalid"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == sent == [] and calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("beside_verdict_line", [True, False], ids=["verdict-line-and-block", "whole-json"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_finding_with_extra_status_field_is_refused_naming_it(beside_verdict_line):
    """The recorder refuses a reply the closeout verifier would reject, naming the field (#12080)."""
    payload = structured_review(1)
    payload["overall"]["correctness"] = "correct"
    payload["findings"][0].update(priority="P3", status="open")
    block = json.dumps(payload, indent=2)
    reply = f"VERDICT: APPROVE\n\n```json\n{block}\n```\n" if beside_verdict_line else block
    with pytest.raises(recorder.RecordError, match=r"schema_violation:findings\.0:.*'status' was unexpected.*allowed"):
        recorder._review_verdict_and_count(reply)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_correct_reply_declaring_missing_capabilities_is_refused():
    """A reviewer that reports missing capabilities cannot approve (#12084)."""
    payload = structured_review(0)
    payload["overall"]["correctness"] = "correct"
    payload["missing_capabilities"] = ["test-execution"]
    with pytest.raises(recorder.RecordError, match=r"'missing_capabilities' was unexpected"):
        recorder._review_verdict_and_count(json.dumps(payload))


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("payload,expected", [
    ("VERDICT: APPROVE\n- prose item\n- another prose item", "APPROVED"),
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_unavailable_structured_count_is_unknown(payload, expected):
    reply = json.dumps(payload) if isinstance(payload, dict) else payload
    assert recorder._review_verdict_and_count(reply) == (expected, "unknown")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("metadata", [
    {"review_mode": "red_team", "unknown_history": ["b" * 40]},
    {"review_profile": "ukrainian", "sources_mcp_call_count": 3},
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_skeleton_preserves_complete_marker_metadata(metadata):
    body = recorder.build_comment(
        sha=SHA, task_id="review-one", started="2026-09-23T12:00:00.000001+00:00",
        verdict="APPROVED", model=CURRENT_SOL, family="openai", reply="",
        withheld_findings=2, **metadata,
    )
    marker = sweep.parse_marker(body)
    assert marker["sha"] == SHA and marker["verdict"] == "APPROVED"
    for name, value in metadata.items():
        assert marker["sources_calls" if name == "sources_mcp_call_count" else name] == value


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_transport_failure_after_scan_never_retries_skeleton(scanned_recorder, tmp_path, monkeypatch):
    from lu_machinery.publish import github

    tasks, comments, calls, sent, _ = scanned_recorder
    attempts = []

    def fail(argv, **kwargs):
        attempts.append(argv)
        raise OSError("synthetic transport failure")

    monkeypatch.setattr(github, "_send", fail)
    with pytest.raises(recorder.RecordError, match="publication unavailable"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert len(attempts) == 1
    assert comments == sent == [] and calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_head_movement_before_skeleton_write_refuses(scanned_recorder, tmp_path, monkeypatch):
    tasks, comments, calls, sent, _ = scanned_recorder
    write_task(tasks, reply="VERDICT: APPROVE\ncredential stored SYNTHETIC-DETAIL")
    heads = iter([SHA, "b" * 40])
    monkeypatch.setattr(recorder, "_pr", lambda *args: {
        "number": 42, "headRefName": "codex/42", "headRefOid": next(heads),
    })
    with pytest.raises(recorder.RecordError, match="PR head moved before publication"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == sent == [] and calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_edited_skeleton_cannot_reconcile(scanned_recorder, tmp_path):
    tasks, comments, calls, sent, _ = scanned_recorder
    write_task(tasks, reply="VERDICT: APPROVE\ncredential stored SYNTHETIC-DETAIL")
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    comments[0]["updated_at"] = "2026-09-23T14:00:00Z"
    with pytest.raises(recorder.RecordError, match="existing verdict marker was edited or conflicts"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert sent == [comments[0]["body"]] and calls == {"posts": 1, "statuses": 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("identity", [
    {"schema_version": 2, "worktree_branch": "claude/private-review", "review_branch": "codex/42", "pinned_head": SHA},
    {"worktree_branch": None, "review_branch": "codex/42", "pinned_head": SHA},
    {},  # Legacy attached record has no review_branch field.
    {"review_branch": "codex/42"},
    {"review_branch": "  codex/42  "},
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_branch_identity_binds_supported_record(monkeypatch, tmp_path, identity):
    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, **identity)
    before = (tasks / "review-one.json").read_bytes()
    receipt = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert receipt["head"] == SHA
    assert receipt["verdict"] == "APPROVED"
    assert receipt["status"] == "posted"
    assert recorder.parse_marker(comments[0]["body"])["sha"] == SHA
    assert calls == {"posts": 1, "statuses": 1}
    assert (tasks / "review-one.json").read_bytes() == before


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("identity,reason", [
    ({"schema_version": 2}, "not branch-pinned"),
    ({"schema_version": 2, "review_branch": None}, "not branch-pinned"),
    ({"worktree_branch": None}, "not branch-pinned"),
    ({"review_branch": "other/branch"}, "conflicting"),
    ({"worktree_branch": None, "review_branch": ""}, "invalid"),
    ({"worktree_branch": None, "review_branch": 42}, "invalid"),
    ({"worktree_branch": 42, "review_branch": "codex/42"}, "invalid"),
    *[({"worktree_branch": None, "review_branch": branch}, "invalid")
      for branch in ("unsafe..branch", "origin/codex/42", "refs/heads/codex/42", "bad:name")],
    *[({"worktree_branch": None, "review_branch": branch}, "protected") for branch in ("main", "master")],
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_bad_branch_identity_refuses_before_network(monkeypatch, tmp_path, identity, reason):
    tasks = tmp_path / "tasks"
    write_task(tasks, **identity)

    def unexpected_network(*args, **kwargs):
        pytest.fail("invalid identity must refuse before network")

    monkeypatch.setattr(recorder, "_run_json", unexpected_network)
    with pytest.raises(recorder.RecordError, match=reason):
        recorder.record("review-one", task_root=tasks, lock_root=tmp_path / "locks")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("pr,reason", [
    ({"branch": "other/branch"}, "branch does not match"),
    ({"head": "b" * 40}, "head moved"),
])
@pytest.mark.parametrize("checkout", [
    {"worktree_branch": None},
    {"schema_version": 2, "worktree_branch": "claude/private-review"},
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_detached_review_refuses_wrong_pr_identity(monkeypatch, tmp_path, pr, reason, checkout):
    tasks, comments, calls = setup_record(monkeypatch, tmp_path, **pr)
    write_task(tasks, review_branch="codex/42", **checkout)
    with pytest.raises(recorder.RecordError, match=reason):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert not comments
    assert calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("updates,families,reason", [
    ({"status": "running"}, {"google"}, "not done"),
    ({"worktree_base_sha": None}, {"google"}, "reviewed SHA"),
    ({"pinned_head": "b" * 40}, {"google"}, "conflicts with pinned head"),
    ({"pinned_head": "a" * 7}, {"google"}, "conflicts with pinned head"),
    ({"agent": "grok", "model": "grok-4.7"}, {"google"}, "unattested"),
    ({"review_risk": "critical", "model": "claude-sonnet-5-5", "agent": "claude"},
     {"google"}, "not qualified"),
    ({}, {"openai"}, "equals an author family"),
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_detached_identity_preserves_specific_review_refusals(monkeypatch, tmp_path, updates, families, reason):
    tasks, comments, calls = setup_record(monkeypatch, tmp_path, families=families)
    write_task(tasks, worktree_branch=None, review_branch="codex/42", **updates)
    with pytest.raises(recorder.RecordError, match=reason):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert not comments
    assert calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("risk", ["low", "medium"])
@pytest.mark.parametrize("profile", ["code", "ukrainian"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_gemini_verdict_records_and_gate_accepts_exact_head(monkeypatch, tmp_path, risk, profile):
    tasks, comments, calls = setup_record(monkeypatch, tmp_path, families={"openai"})
    write_task(tasks, agent="agy", model="gemini-4-neon-high", review_profile=profile,
               review_risk=risk, sources_mcp_call_count=3 if profile == "ukrainian" else None)
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED"
    marker = recorder.parse_marker(comments[0]["body"])
    assert marker["family"] == "google"
    assert marker["sha"] == SHA
    # An OpenAI author needs only this Gemini seat, on either profile.
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "APPROVED"
    assert keeper._recorded_approval_for_head(comments, SHA, "fleet")
    assert not keeper._recorded_approval_for_head(comments, "b" * 40, "fleet")
    assert calls == {"posts": 1, "statuses": 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("risk,path", [("critical", "ordinary.py"),
                                      ("low", "scripts/review/reviewer_resolver.py"),
                                      ("medium", ".github/workflows/ci.yml")])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_gemini_code_verdict_refused_before_publication(monkeypatch, tmp_path, risk, path):
    from tests.review.record_support import facts_for

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_args, **_kwargs: facts_for({"anthropic"}, changed_paths=(path,)))
    # NEON-SOLTIER-1120: native AGY Gemini 4 Neon High is admitted at critical and on security paths.
    write_task(tasks, agent="agy", model="gemini-4-neon-high", review_profile="code", review_risk=risk,
               reply=attested_approval())
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED"
    assert calls == {"posts": 1, "statuses": 1}

    # Non-native routes carrying a Gemini model remain refused before publication.
    tasks2, comments2, calls2 = setup_record(monkeypatch, tmp_path / "non-native")
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_args, **_kwargs: facts_for({"anthropic"}, changed_paths=(path,)))
    write_task(tasks2, agent="codex", model="gemini-4-neon-high", review_profile="code", review_risk=risk)
    with pytest.raises(recorder.RecordError, match="not a catalog review candidate"):
        recorder.record("review-one", pr_number=42, task_root=tasks2, lock_root=tmp_path / "locks2")
    assert not comments2
    assert calls2 == {"posts": 0, "statuses": 0}

    # Same-family Google author remains refused before publication.
    tasks3, comments3, calls3 = setup_record(monkeypatch, tmp_path / "same-family")
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_args, **_kwargs: facts_for({"google"}, changed_paths=(path,)))
    write_task(tasks3, agent="agy", model="gemini-4-neon-high", review_profile="code", review_risk=risk)
    with pytest.raises(recorder.RecordError, match="reviewer family equals an author family"):
        recorder.record("review-one", pr_number=42, task_root=tasks3, lock_root=tmp_path / "locks3")
    assert not comments3
    assert calls3 == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("head", ["a" * 7, "b" * 40])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_gemini_stale_or_short_head_refused(monkeypatch, tmp_path, head):
    tasks, _, calls = setup_record(monkeypatch, tmp_path, head=head)
    write_task(tasks, agent="agy", model="gemini-4-neon-high", review_risk="low")
    with pytest.raises(recorder.RecordError):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("sha", ["a" * 7, "a" * 39, "z" * 40])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_gemini_marker_requires_full_commit_sha(sha):
    with pytest.raises(recorder.RecordError, match="full 40-hex"):
        recorder.build_comment(sha=sha, task_id="gemini-review", started="2026-10-09T00:00:00Z",
                               verdict="APPROVED", model="gemini-3.8-flash-high", family="google", reply="VERDICT: APPROVE")


@pytest.fixture()
def recorder_matcher(synthetic_opsec):
    from tests.opsec_fixtures import synthetic_rules

    rules = synthetic_rules(rule="3-absolute-path", level=3, pattern=r"(?<![<\w:])/[A-Za-z][^\s`'\"<>),;\]}|*]*")
    (synthetic_opsec / "rules.json").write_text(json.dumps(rules))


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize(
    "classification",
    [
        {"review_profile": "ukrainian"},
        {"review_profile": " Ukrainian "},
        {"review_language_lane": True},
        {"language_lane": True},
        {"research_task_family": "ukrainian-review"},
        {"task_family": "ukrainian-authoring"},
    ],
)
@pytest.mark.usefixtures("agent_runtime_roots")
def test_ukrainian_review_with_sources_calls_accepted(monkeypatch, tmp_path, count, classification):
    from lu_machinery.dispatcher.worker import _receipt_failure

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    statuses = []
    original_status = recorder.post_commit_status

    def capture_status(**kwargs):
        statuses.append(kwargs)
        original_status(**kwargs)

    monkeypatch.setattr(recorder, "post_commit_status", capture_status)
    excerpt = 'VESUM fixture tag: "example".'
    captured_source_trace(tasks, output=excerpt)
    reply = (
        "VERDICT: APPROVE\n\n```json\n"
        + json.dumps({"summary": "The edited examples are correct.", "sources": excerpt})
        + "\n```\n"
    )
    assert _receipt_failure({"schema_version": 2, "require_review_verdict": True, **classification}, reply) is None
    write_task(tasks, reply=reply, sources_mcp_call_count=count, **classification)
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED"
    assert result["status"] == "posted"
    assert len(comments) == 1
    assert calls == {"posts": 1, "statuses": 1}
    assert f"Sources MCP calls: {count}\n" in comments[0]["body"].split("<details>", 1)[0]
    assert f"sources_calls={count}" in comments[0]["body"].splitlines()[-1]
    marker = recorder.parse_marker(comments[0]["body"])
    assert marker["verdict"] == "APPROVED"
    assert "coverage" not in marker and "denominator" not in marker
    assert marker["sources_calls"] == count
    assert marker["review_profile"] == "ukrainian"
    assert f"sources={count}" in statuses[0]["description"]
    # A Google author needs only this Sol seat, and the keeper can still revoke that approval.
    assert statuses[0]["state"] == "success"
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "APPROVED"
    assert keeper._recorded_approval_for_head(comments, SHA, "fleet")
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert calls == {"posts": 1, "statuses": 2}


def record_seat(tasks, tmp_path, task_id, **seat):
    """Record one Ukrainian-profile review of SHA through the recorder."""
    write_task(tasks, task_id=task_id, review_profile="ukrainian", sources_mcp_call_count=3, **seat)
    recorder.record(task_id, pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")


def merge(monkeypatch, comments, *, files=None):
    """Run ``pr-merge`` on SHA with every check passing; only the recorded seats can refuse it."""
    from lu_machinery.opsec import prepublish as gate
    from lu_machinery.publish import github as pub
    from tests.opsec_fixtures import CATALOG

    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)
    writes = []

    def spy(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            body = {"number": 42, "isDraft": False, "headRefOid": SHA, "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [{"__typename": "CheckRun", "name": "CI Gate", "workflowName": "CI",
                                           "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        elif args[1:3] == ["pr", "checks"]:
            body = [{"name": "CI Gate", "bucket": "pass", "workflow": "CI"}]
        elif args[1:5] == ["api", "--method", "POST", "graphql"]:
            body = {"data": {"repository": {"pullRequest": {
                "id": "PR_fixture_42", "headRefOid": SHA, "isMergeQueueEnabled": False,
                "viewerMergeHeadlineText": "clean (#42)", "viewerMergeBodyText": "clean"}}}}
        elif args[1:4] == ["api", "--method", "GET"]:
            if "/issues/42/comments" in args[4]:
                body = [comments]
            elif "/pulls/42/files" in args[4]:
                body = [files if files is not None else [{"filename": "scripts/example.py"}]]
            else:
                body = {"login": "fleet"}
        else:
            writes.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    pub.publish("pr-merge", repo="unit/public", number=42, runner=spy, env={})
    return writes


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_metadata_only_release_manifest_merges_without_a_ukrainian_seat(monkeypatch, tmp_path):
    from lu_machinery.ci.deploy_paths import decide_auto_deploy
    from lu_machinery.opsec import prepublish as gate

    # PR #12629 at 02b0a5e3: the manifest is its only content-prefixed path.
    paths = [
        "docs/runbooks/word-atlas-static-api.md",
        "registry/lexicon/atlas-runtime-release.json",
        "scripts/audit/generate_search_index.py",
        "scripts/deploy/prepare_atlas_runtime_release.py",
        "scripts/deploy/runtime_source_report.py",
        "tests/test_atlas_runtime_release.py",
        "tests/test_generate_search_index.py",
    ]
    tasks, comments, _ = setup_record(monkeypatch, tmp_path, families={"openai"})
    write_task(tasks, agent="claude", model="claude-opus-5-5", review_profile="infra", review_risk="critical",
               reply=attested_approval())
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert sweep.lookup_verdict(comments, SHA, "fleet").ukrainian_seats == ()

    payload = "curriculum/l2-uk-en/a2/example.mdx"
    rows = [{"filename": path} for path in paths]
    for files, needs_ukrainian in (
        (rows, False),
        ([*rows, {"filename": payload}], True),
        ([*rows, {"filename": paths[1] + "l"}], True),
        ([{**row, "status": "renamed", "previous_filename": payload} if row["filename"] == paths[1]
          else row for row in rows], True),
    ):
        classified = sweep.pr_file_paths(files)
        assert sweep.requires_ukrainian_behavior_receipt(classified) is needs_ukrainian
        verdict = sweep.lookup_verdict(
            comments, SHA, "fleet", learner_content=sweep.changes_learner_content(classified),
        )
        assert verdict.state == (sweep.NEEDS_SEAT if needs_ukrainian else "APPROVED")
        if needs_ukrainian:
            with pytest.raises(gate.PublishBlocked, match="Ukrainian review seat has not approved"):
                merge(monkeypatch, comments, files=files)
        else:
            assert len(merge(monkeypatch, comments, files=files)) == 1
        assert decide_auto_deploy(classified).reason == "content_drift"


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_one_ukrainian_seat_leaves_the_status_pending_and_the_merge_refused(monkeypatch, tmp_path):
    # An author outside OpenAI and Google still needs both Gemini and Sol.
    from lu_machinery.opsec import prepublish as gate

    tasks, comments, _ = setup_record(monkeypatch, tmp_path, families={"anthropic"})
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **kwargs: statuses.append(kwargs))
    record_seat(tasks, tmp_path, "review-one", agent="agy", model="gemini-4-neon-high")
    assert statuses[-1]["state"] == "pending"
    assert statuses[-1]["description"].endswith("; awaiting Ukrainian seat: openai")
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == sweep.NEEDS_SEAT
    with pytest.raises(gate.PublishBlocked, match="Ukrainian review seat has not approved"):
        merge(monkeypatch, comments)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_two_eligible_ukrainian_seats_at_the_same_head_pass(monkeypatch, tmp_path):
    # An author outside OpenAI and Google is approved once Gemini and Sol have both approved.
    tasks, comments, _ = setup_record(monkeypatch, tmp_path, families={"anthropic"})
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **kwargs: statuses.append(kwargs))
    record_seat(tasks, tmp_path, "review-one", agent="agy", model="gemini-4-neon-high")
    record_seat(tasks, tmp_path, "review-two", started_at="2026-09-23T12:00:01.000001+00:00")
    assert [status["state"] for status in statuses] == ["pending", "success"]
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "APPROVED"
    assert len(merge(monkeypatch, comments)) == 1


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_a_ukrainian_seat_approving_an_older_head_does_not_count(monkeypatch, tmp_path):
    # Seats count only when they approved the head that would land.
    from lu_machinery.opsec import prepublish as gate
    from tests.review.record_support import OTHER

    tasks, comments, _ = setup_record(monkeypatch, tmp_path, families={"anthropic"})
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **kwargs: statuses.append(kwargs))
    older = recorder.build_comment(
        sha=OTHER, task_id="review-old", started="2026-09-23T11:00:00.000001+00:00", verdict="APPROVED",
        model=CURRENT_SOL, family="openai", reply="VERDICT: APPROVE",
        review_profile="ukrainian", sources_mcp_call_count=3,
        author_models=("gemini-3.8-flash-high",),
    )
    stamp = "2026-09-23T12:30:00Z"
    comments.append({"id": 0, "body": older, "user": {"login": "fleet"}, "author_association": "MEMBER",
                     "created_at": stamp, "updated_at": stamp})
    record_seat(tasks, tmp_path, "review-one", agent="agy", model="gemini-4-neon-high")
    assert statuses[-1]["state"] == "pending"
    assert statuses[-1]["description"].endswith("; awaiting Ukrainian seat: openai")
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == sweep.NEEDS_SEAT
    with pytest.raises(gate.PublishBlocked, match="Ukrainian review seat has not approved"):
        merge(monkeypatch, comments)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_sol_authored_gemini_approval_passes_and_gemini_author_needs_sol(monkeypatch, tmp_path):
    """A Sol-authored Ukrainian head with one Gemini APPROVE passes; a Gemini-authored one needs Sol."""
    from lu_machinery.opsec import prepublish as gate

    sol_root = tmp_path / "sol"
    tasks, comments, _ = setup_record(monkeypatch, sol_root, families={"openai"})
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **kwargs: statuses.append(kwargs))
    record_seat(tasks, sol_root, "review-gemini", agent="agy", model="gemini-4-neon-high")
    assert statuses[-1]["state"] == "success"
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == "APPROVED"
    assert len(merge(monkeypatch, comments)) == 1

    gemini_root = tmp_path / "gemini"
    _tasks, comments, _calls = setup_record(monkeypatch, gemini_root, families={"google"})
    body = recorder.build_comment(
        sha=SHA, task_id="review-gemini", started="2026-09-23T12:00:00.000001+00:00",
        verdict="APPROVED", model="gemini-4-neon-high", family="google", reply="VERDICT: APPROVE",
        review_profile="ukrainian", sources_mcp_call_count=3,
        author_models=("gemini-3.8-flash-high",),
    )
    stamp = "2026-09-23T13:00:00Z"
    comments.append({
        "id": 1, "body": body, "user": {"login": "fleet"}, "author_association": "MEMBER",
        "created_at": stamp, "updated_at": stamp,
    })
    verdict = sweep.lookup_verdict(comments, SHA, "fleet")
    assert verdict.state == sweep.NEEDS_SEAT
    assert verdict.ukrainian_seats == ("google",)
    with pytest.raises(gate.PublishBlocked, match="Ukrainian review seat has not approved"):
        merge(monkeypatch, comments)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_a_blank_author_beside_an_attested_marker_still_needs_both_seats():
    """One missing attestation is not dropped when another marker names an author."""
    known = {"author_models": (CURRENT_SOL,)}
    blanks = (
        {},
        {"author_models": None},
        {"author_models": ()},
        {"author_models": []},
        {"author_models": ""},
        {"author_models": {}},
        {"author_models": 0},
        {"author_models": False},
    )
    for blank in blanks:
        assert required_ukrainian_seats(attested_author_families([blank, known])) == UKRAINIAN_SEAT_FAMILIES

    def comment(body, number):
        stamp = "2026-09-23T13:00:00Z"
        return {
            "id": number, "body": body, "user": {"login": "fleet"}, "author_association": "MEMBER",
            "created_at": stamp, "updated_at": stamp,
        }

    bare = recorder.build_comment(
        sha=SHA, task_id="review-bare", started="2026-09-23T12:00:00.000001+00:00",
        verdict="APPROVED", model="gemini-4-neon-high", family="google", reply="VERDICT: APPROVE",
        review_profile="ukrainian", sources_mcp_call_count=3,
    )
    named = recorder.build_comment(
        sha=SHA, task_id="review-named", started="2026-09-23T12:00:01.000001+00:00",
        verdict="APPROVED", model="gemini-4-neon-high", family="google", reply="VERDICT: APPROVE",
        review_profile="ukrainian", sources_mcp_call_count=3, author_models=(CURRENT_SOL,),
    )
    alone = sweep.lookup_verdict([comment(bare, 1)], SHA, "fleet")
    assert alone.state == sweep.NEEDS_SEAT
    combined = sweep.lookup_verdict([comment(bare, 1), comment(named, 2)], SHA, "fleet")
    assert combined.state == sweep.NEEDS_SEAT
    assert combined.ukrainian_seats == ("google",)


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("alias", [
    {"review_language_lane": True},
    {"language_lane": True},
    {"research_task_family": "ukrainian-review"},
    {"research_task_family": "ukrainian-authoring"},
    {"task_family": "ukrainian-review"},
    {"task_family": "ukrainian-authoring"},
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_a_claude_ukrainian_alias_cannot_record_a_verdict(monkeypatch, tmp_path, alias):
    """A code-profile task that the lane still classifies as Ukrainian needs a seat."""
    tasks, comments, calls = setup_record(monkeypatch, tmp_path, families={"openai"})
    write_task(
        tasks, reply="VERDICT: REQUEST_CHANGES", agent="claude", model="claude-opus-5-5",
        review_profile="code", sources_mcp_call_count=3, **alias,
    )
    with pytest.raises(recorder.RecordError, match="Gemini or Sol"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert not comments
    assert calls == {"posts": 0, "statuses": 0}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES", "BLOCKED"])
@pytest.mark.parametrize("count", [0, None, -1, True, "1", 1.0])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_ukrainian_review_without_known_positive_count_refused_before_network(
    monkeypatch, tmp_path, verdict, count
):
    tasks = tmp_path / "tasks"
    write_task(tasks, reply=f"VERDICT: {verdict}", review_profile="ukrainian", sources_mcp_call_count=count)

    def unexpected_network(*args, **kwargs):
        pytest.fail("Sources evidence must be checked before GitHub lookup or publication")

    monkeypatch.setattr(recorder, "_run_json", unexpected_network)
    reason = "zero Sources MCP calls" if count == 0 else "call count unknown"
    with pytest.raises(recorder.RecordError, match=reason):
        recorder.record("review-one", task_root=tasks, lock_root=tmp_path / "locks")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_missing_runtime_count_cannot_be_replaced_by_reviewer_prose(tmp_path):
    tasks = tmp_path / "tasks"
    write_task(tasks, review_profile="ukrainian", reply="Used mcp__sources__verify_words.\nVERDICT: APPROVE")
    with pytest.raises(recorder.RecordError, match="call count unknown"):
        recorder.record("review-one", task_root=tasks, lock_root=tmp_path / "locks")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("count", [0, None])
@pytest.mark.parametrize(
    "classification",
    [
        {"review_language_lane": True},
        {"language_lane": True},
        {"research_task_family": "ukrainian-review"},
        {"task_family": "ukrainian-authoring"},
    ],
)
@pytest.mark.usefixtures("agent_runtime_roots")
def test_task_classification_cannot_bypass_sources_gate(tmp_path, count, classification):
    tasks = tmp_path / "tasks"
    write_task(tasks, review_profile="code", sources_mcp_call_count=count, **classification)
    with pytest.raises(recorder.RecordError, match="Ukrainian review"):
        recorder.record("review-one", task_root=tasks, lock_root=tmp_path / "locks")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("mode", ["cross_family", "red_team"])
@pytest.mark.parametrize("profile,count", [("code", None), ("code", 0), ("code", 4), ("ukrainian", 4)])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_parse_marker_extracts_optional_integer_count(mode, profile, count):
    body = recorder.build_comment(
        sha=SHA, task_id="review-one", started="2026-09-23T12:00:00.000001+00:00",
        verdict="APPROVED", model=CURRENT_SOL, family="openai", reply="VERDICT: APPROVE",
        review_mode=mode, review_profile=profile, sources_mcp_call_count=count,
    )
    marker = recorder.parse_marker(body)
    assert marker["sources_calls"] == count
    assert count is None or type(marker["sources_calls"]) is int


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("mode", ["cross_family", "red_team"])
@pytest.mark.parametrize("verdict", ["APPROVED", "APPROVE"])
@pytest.mark.parametrize("count", [None, "0", "-1", "unknown", "True", "1.0"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_ukrainian_marker_without_positive_count_refused(monkeypatch, tmp_path, mode, verdict, count):
    tasks, comments, _ = setup_record(monkeypatch, tmp_path)
    write_task(tasks, review_profile="ukrainian", sources_mcp_call_count=3)
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    valid = comments[0]
    body = recorder.build_comment(
        sha=SHA, task_id="later", started="2026-09-23T12:00:01.000001+00:00",
        verdict=verdict, model=CURRENT_SOL, family="openai", reply=f"VERDICT: {verdict}",
        review_mode=mode, review_profile="ukrainian", sources_mcp_call_count=3,
    )
    body = body.replace(" sources_calls=3", "" if count is None else f" sources_calls={count}")
    body = body.replace("Sources MCP calls: 3\n", "" if count is None else f"Sources MCP calls: {count}\n")
    invalid = {**valid, "body": body}
    assert recorder.parse_marker(body) is None
    assert sweep.lookup_verdict([valid, invalid], SHA, "fleet").state == "unknown"
    assert not keeper._recorded_approval_for_head([invalid], SHA, "fleet")
    assert not keeper._ever_approved([invalid], "fleet")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("replacement", ["sources_calls=2", "sources_calls=3 sources_calls=3"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_sources_marker_must_match_published_count(replacement):
    body = recorder.build_comment(
        sha=SHA, task_id="review-one", started="2026-09-23T12:00:00.000001+00:00",
        verdict="APPROVED", model=CURRENT_SOL, family="openai", reply="VERDICT: APPROVE",
        review_profile="ukrainian", sources_mcp_call_count=3,
    ).replace("sources_calls=3", replacement)
    assert recorder.parse_marker(body) is None


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_removing_profile_marker_cannot_hide_missing_sources_count():
    body = recorder.build_comment(
        sha=SHA, task_id="review-one", started="2026-09-23T12:00:00.000001+00:00",
        verdict="APPROVED", model=CURRENT_SOL, family="openai", reply="VERDICT: APPROVE",
        review_profile="ukrainian", sources_mcp_call_count=3,
    ).replace(" review_profile=ukrainian sources_calls=3", "")
    assert recorder.parse_marker(body) is None


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_changed_runtime_count_conflicts_with_existing_marker(monkeypatch, tmp_path):
    tasks, _, calls = setup_record(monkeypatch, tmp_path)
    write_task(tasks, review_profile="ukrainian", sources_mcp_call_count=3)
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    write_task(tasks, review_profile="ukrainian", sources_mcp_call_count=4)
    with pytest.raises(recorder.RecordError, match="conflicts with task"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert calls == {"posts": 1, "statuses": 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"], ids=["NEL", "LS", "PS"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_commit_headers_split_only_on_physical_newlines(tmp_path, separator):
    # #9556: a free-text author name must not forge a parent header line.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True, env=env, timeout=30
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    forged = "f" * 40
    git("config", "user.name", f"Name{separator}parent {forged}")
    git("commit", "-q", "--allow-empty", "-m", "subject")
    sha = git("rev-parse", "HEAD")

    [entry] = recorder._read_commit_entries(tmp_path, [sha], deadline=time.monotonic() + 30)

    assert entry["parents"] == []
    assert entry["commit"]["tree"]["sha"] == git("rev-parse", "HEAD^{tree}")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("score_line,score", [
    ("SCORE: 4/10 open P1 on the parser.", 4),
    ("SCORE: 4.5/10", None),
    ("", None),
])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_recorded_round_stores_score_with_reviewer_and_author_models(scanned_recorder, tmp_path, score_line, score):
    """C-3020: the score is advisory; a missing or malformed one is recorded as absent."""
    tasks, comments, _, sent, _ = scanned_recorder
    write_task(tasks, reply=f"VERDICT: REQUEST_CHANGES\n{score_line}\nF1 [P1] a.py:1 fails.\n")
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    body = comments[0]["body"]
    assert sent == [body]
    assert result["score"] == score and result["verdict"] == "CHANGES_REQUESTED"
    assert (
        f"Reviewer family: openai\nVERDICT: CHANGES_REQUESTED\nReviewer model: {CURRENT_SOL}\n"
        f"Review score: {f'{score}/10' if score else 'absent'}\nAuthored by: gemini-3.8-flash-high\n"
    ) in body
    marker = sweep.parse_marker(body)
    assert (marker["verdict"], marker["model"], marker["family"]) == ("CHANGES_REQUESTED", CURRENT_SOL, "openai")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_quote_mismatch_voids_chunk_coverage(monkeypatch, tmp_path):
    """A quote that is not a literal substring of the frozen item does not count as coverage."""
    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import render_item_packet

    tasks, comments, _ = setup_record(monkeypatch, tmp_path)
    captured_source_trace(tasks)
    packet = build_item_packet([{"id": "card-a", "text": "привіт"}, {"id": "card-b", "text": "друг"}])
    prompt = tmp_path / "brief.md"
    prompt.write_bytes(render_item_packet(packet).encode())
    reply = (
        "VERDICT: APPROVE\n"
        "```ukrainian-item-judgments\n"
        + json.dumps(
            {
                "schema": "ukrainian-item-judgments.v1",
                "items": [
                    {
                        "id": "card-a",
                        "verdict": "APPROVE",
                        "finding": "",
                        "sources": "VESUM",
                        "quote": "немає",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n```\n"
    )
    assert "немає" not in packet["items"][0]["text"]
    write_task(
        tasks,
        reply=reply,
        review_profile="ukrainian",
        sources_mcp_call_count=3,
        prompt_file=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
    )
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    marker = sweep.parse_marker(comments[0]["body"])
    assert marker["void"] == "quote-mismatch"
    assert marker["coverage"] == []
    assert marker["denominator"] == ["card-a", "card-b"]
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == sweep.NEEDS_COVERAGE

    hidden = reply.replace(
        '"quote": "немає"',
        '"finding": "Frozen input quote: \\"немає\\"."',
    )
    write_task(
        tasks,
        task_id="review-hidden",
        reply=hidden,
        review_profile="ukrainian",
        sources_mcp_call_count=3,
        prompt_file=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
    )
    captured_source_trace(tasks, task_id="review-hidden")
    recorder.record("review-hidden", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    hidden_marker = sweep.parse_marker(comments[1]["body"])
    assert hidden_marker["void"] == "quote-mismatch"
    assert hidden_marker["coverage"] == []

    single = reply.replace(
        '"quote": "немає"',
        '"finding": "Frozen input quote: \'немає\'."',
    )
    write_task(
        tasks,
        task_id="review-single",
        reply=single,
        review_profile="ukrainian",
        sources_mcp_call_count=3,
        prompt_file=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
    )
    captured_source_trace(tasks, task_id="review-single")
    recorder.record("review-single", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    single_marker = sweep.parse_marker(comments[2]["body"])
    assert single_marker["void"] == "quote-mismatch"
    assert single_marker["coverage"] == []

    apostrophe = reply.replace(
        '"quote": "немає"',
        '"finding": "Frozen input quote: \'м\'ясо\'."',
    )
    write_task(
        tasks,
        task_id="review-apostrophe",
        reply=apostrophe,
        review_profile="ukrainian",
        sources_mcp_call_count=3,
        prompt_file=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
    )
    captured_source_trace(tasks, task_id="review-apostrophe")
    recorder.record("review-apostrophe", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    apostrophe_marker = sweep.parse_marker(comments[3]["body"])
    assert apostrophe_marker["void"] == "quote-mismatch"
    assert apostrophe_marker["coverage"] == []

    spaces = reply.replace(
        '"quote": "немає"',
        '"finding": "Frozen input quote: \' \'. "',
    )
    write_task(
        tasks,
        task_id="review-spaces",
        reply=spaces,
        review_profile="ukrainian",
        sources_mcp_call_count=3,
        prompt_file=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
    )
    captured_source_trace(tasks, task_id="review-spaces")
    recorder.record("review-spaces", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    spaces_marker = sweep.parse_marker(comments[4]["body"])
    assert spaces_marker["void"] == "quote-mismatch"
    assert spaces_marker["coverage"] == []


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_block_only_ukrainian_reply_records_full_coverage(monkeypatch, tmp_path):
    """Defect class (#12417): a contract-only judgment block with a frozen packet is refused for lack of a verdict line."""
    from lu_machinery.dispatcher.worker import _receipt_failure
    from lu_machinery.review.review_contract import build_item_packet, render_item_packet

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    packet = build_item_packet([{"id": "card-a", "text": "привіт"}, {"id": "card-b", "text": "друг"}])
    prompt = tmp_path / "brief.md"
    prompt.write_bytes(render_item_packet(packet).encode())
    excerpt = 'VESUM fixture tag: "example".'
    items = [
        {"id": item_id, "verdict": "APPROVE", "finding": "none", "sources": excerpt, "quote": text}
        for item_id, text in (("card-a", "привіт"), ("card-b", "друг"))
    ]
    reply = (
        "```ukrainian-item-judgments\n"
        + json.dumps({"schema": "ukrainian-item-judgments.v1", "items": items}, ensure_ascii=False)
        + "\n```\n"
    )
    review = {
        "review_profile": "ukrainian",
        "sources_mcp_call_count": 1,
        "prompt_file": str(prompt),
        "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
    }
    captured_source_trace(tasks, output=excerpt)
    write_task(tasks, reply=reply + '```json\n{"summary":"Extra payload"}\n```\n', **review)
    with pytest.raises(recorder.RecordError, match="CLAIM_PAYLOAD_COUNT"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and calls == {"posts": 0, "statuses": 0}

    assert _receipt_failure({"schema_version": 2, "require_review_verdict": True, **review}, reply) is None
    write_task(tasks, reply=reply, **review)
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    marker = sweep.parse_marker(comments[0]["body"])
    assert result["verdict"] == marker["verdict"] == "APPROVED"
    assert marker["denominator"] == marker["coverage"] == ["card-a", "card-b"]
    assert marker.get("void") is None


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_item_judgments_unknown_verdict_leaves_coverage_incomplete(monkeypatch, tmp_path):
    """An item with verdict 'unknown' is unreviewed and does not count as coverage."""
    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import render_item_packet

    tasks, comments, _ = setup_record(monkeypatch, tmp_path)
    captured_source_trace(tasks)
    packet = build_item_packet([{"id": "card-a", "text": "привіт"}, {"id": "card-b", "text": "друг"}])
    prompt = tmp_path / "brief.md"
    prompt.write_bytes(render_item_packet(packet).encode())
    reply = (
        "VERDICT: APPROVE\n"
        "```ukrainian-item-judgments\n"
        + json.dumps(
            {
                "schema": "ukrainian-item-judgments.v1",
                "items": [
                    {
                        "id": "card-a",
                        "verdict": "APPROVE",
                        "finding": "",
                        "sources": "VESUM",
                        "quote": "привіт",
                    },
                    {
                        "id": "card-b",
                        "verdict": "unknown",
                        "finding": "Evidence inconclusive",
                        "sources": "none",
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n```\n"
    )
    write_task(
        tasks,
        reply=reply,
        review_profile="ukrainian",
        sources_mcp_call_count=3,
        prompt_file=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
    )
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    marker = sweep.parse_marker(comments[0]["body"])
    assert marker["denominator"] == ["card-a", "card-b"]
    assert marker["coverage"] == ["card-a"]
    assert "card-b" not in marker["coverage"]
    assert sweep.lookup_verdict(comments, SHA, "fleet").state == sweep.NEEDS_COVERAGE


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_code_review_with_evidence_yields_reviewer_bookkeeping_empty_coverage():
    """Code profile rejects evidence in item judgments as reviewer bookkeeping, returning empty coverage."""
    from lu_machinery.review.review_contract import VOID_REVIEWER_BOOKKEEPING
    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import judge_item_reply

    packet = build_item_packet([{"id": "item-1", "text": "def test(): pass"}])
    reply = (
        "VERDICT: APPROVE\n"
        "```ukrainian-item-judgments\n"
        + json.dumps({
            "schema": "ukrainian-item-judgments.v1",
            "items": [
                {
                    "id": "item-1",
                    "verdict": "APPROVE",
                    "finding": "Looks good",
                    "sources": "none",
                    "quote": "def test(): pass",
                    "evidence": [{"receipt": "rr-1", "quote": "pass"}],
                }
            ],
        })
        + "\n```\n"
    )
    chunk = judge_item_reply(packet, reply, stated="APPROVED", ukrainian=False)
    assert chunk.void == VOID_REVIEWER_BOOKKEEPING
    assert chunk.coverage == ()


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.parametrize("claim,output,placement", [
    ("Example Reference page 7", "Different Reference page 2", "sources"),
    ("Rule 8 classifies this suffix as soft-group.", "Rule 8 classifies this suffix as hard-group.", "finding"),
    ("VESUM tag noun:inanim:f:v_naz for example", "VESUM tag noun:anim:f:v_naz for example", "sources"),
    ("check_russian_shadow for example returned confidence 0.0", "check_russian_shadow for other returned 0.0", "sources"),
    ("Example Reference page 7", None, "sources"),
    ("СУМ-20 подає цю форму як нормативну, с. 45", "VESUM", "finding"),
    ("verify_stress returned stress on the second syllable", "VESUM", "finding"),
    ("с. 45", "VESUM", "finding"),
    ("Example Reference page 7", "Example Reference page 75", "sources"),
    ("Rule 8", "Rule 81", "finding"),
    ("noun:inanim:f:v_naz", "noun:anim:f:v_naz", "finding"),
    ("ВТС подає це значення як основне", "VESUM", "finding"),
    ("стор. 45", "VESUM", "finding"),
    ("Заболотний подає цю форму", "VESUM", "finding"),
    ("Вікіпедія подає цю форму", "VESUM", "finding"),
    ("Wikipedia reports this form", "VESUM", "finding"),
], ids=["source-page", "rule", "tag", "check", "missing-output", "ukrainian-source", "tool-name", "ukrainian-page",
        "page-prefix", "rule-prefix", "bare-tag", "vts", "page-abbreviation", "author-cyrillic", "wikipedia-uk", "wikipedia-en"])
@pytest.mark.parametrize("reply_format", ["items", "overall", "finding", "title", "smallest_fix", "body", "prose",
                                         "verbatim", "nested", "top-notes", "fenced-summary", "fenced-quote", "fenced-list",
                                         "key-json", "plain-mapping", "nested-control", "non-row-item", "invalid-control-types",
                                         "dotted-reference"])
@pytest.mark.usefixtures("agent_runtime_roots")
def test_unbound_ukrainian_source_claims_refused_before_publication(
    monkeypatch, tmp_path, claim, output, placement, reply_format,
):
    """An unbound review must not publish a source assertion missing from its own outputs."""
    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import render_item_packet

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    packet = build_item_packet([{"id": "item-a", "text": "example"}])
    prompt = tmp_path / "brief.md"
    prompt.write_text(render_item_packet(packet))
    row = {"id": "item-a", "verdict": "APPROVE", "finding": "Appropriate activity.", "sources": "none"}
    row[placement] = claim
    reply = "VERDICT: APPROVE\n```ukrainian-item-judgments\n" + json.dumps({
        "schema": "ukrainian-item-judgments.v1", "items": [row],
    }) + "\n```\n"
    audit_id, audit_field = "item-a", placement
    audit_claim = claim
    if reply_format == "key-json":
        reply = "VERDICT: APPROVE\n```json\n" + json.dumps({claim: "Uncited wording."}) + "\n```\n"
        audit_id, audit_field = None, claim + ".__key__"
    elif reply_format == "plain-mapping":
        reply = "VERDICT: APPROVE\n" + claim + ": Uncited wording.\n"
        audit_id, audit_field = "review-prose", "finding"
        audit_claim = "VERDICT: APPROVE\n" + claim + ": Uncited wording."
    elif reply_format == "nested-control":
        payload = {"notes": {"category": claim, "outcome": claim, "id": claim,
                             "location": {"path": claim}, "receipt_id": claim}}
        reply = "VERDICT: APPROVE\n```json\n" + json.dumps(payload) + "\n```\n"
        audit_id, audit_field = None, "notes.category"
    elif reply_format == "non-row-item":
        reply = "VERDICT: APPROVE\n```json\n" + json.dumps({"items": [claim]}) + "\n```\n"
        audit_id, audit_field = None, "items"
    elif reply_format == "invalid-control-types":
        payload = {"items": [{"id": {"claim": claim}, "quote": {"claim": claim}}],
                   "findings": [{"location": {"path": {"claim": claim}}}]}
        reply = "VERDICT: APPROVE\n```yaml\n" + json.dumps(payload) + "\n```\n"
        audit_id, audit_field = {"claim": claim}, "id.claim"
    elif reply_format == "dotted-reference":
        payload = {"findings": [{"source_conflict.a": {"quote": claim, "receipt_id": "step-4"}}]}
        reply = "VERDICT: APPROVE\n```yaml\n" + json.dumps(payload) + "\n```\n"
        audit_id, audit_field = None, "source_conflict.a.quote"
    elif reply_format in {"fenced-summary", "fenced-quote", "fenced-list"}:
        field = "quote" if reply_format == "fenced-quote" else "summary"
        body = "- " + claim if reply_format == "fenced-list" else json.dumps({field: claim})
        tag = "yaml" if reply_format == "fenced-list" else "json"
        reply = f"VERDICT: APPROVE\n```{tag}\n{body}\n```\n"
        if reply_format == "fenced-list":
            audit_id, audit_field = "review-prose", "finding"
            audit_claim = "VERDICT: APPROVE\n\n\n" + body
        else:
            audit_id, audit_field = None, field
    elif reply_format in {"nested", "top-notes"}:
        row[placement] = "none"
        payload = {"schema": "ukrainian-item-judgments.v1", "items": [row]}
        if reply_format == "nested":
            row["extra"] = {"notes": [claim]}
            audit_id, audit_field = "item-a", "extra.notes"
        else:
            payload["notes"] = claim
            audit_id, audit_field = None, "notes"
        reply = "VERDICT: APPROVE\n```ukrainian-item-judgments\n" + json.dumps(payload) + "\n```\n"
    elif reply_format == "prose":
        row[placement] = "none"
        reply = claim + ";\nVERDICT: APPROVE\n```ukrainian-item-judgments\n" + json.dumps({
            "schema": "ukrainian-item-judgments.v1", "items": [row],
        }) + "\n```\n"
        audit_id, audit_field = "review-prose", "finding"
    elif reply_format != "items":
        payload = structured_review(0 if reply_format == "overall" else 1)
        payload["overall"]["correctness"] = "correct"
        if reply_format == "overall":
            payload["overall"]["explanation"] = claim
            audit_id, audit_field = None, "overall.explanation"
        else:
            payload["overall"]["explanation"] = "Appropriate activity."
            payload["findings"][0].update(priority="P3", body="Advisory wording.", sources=["none"])
            audit_field = "sources" if reply_format == "finding" else reply_format
            payload["findings"][0][audit_field] = [claim] if audit_field == "sources" else claim
            audit_id = "F1"
        reply = json.dumps(payload)
    captured_source_trace(tasks, arguments={"query": claim}, output=output)
    write_task(tasks, reply=reply, review_profile="ukrainian", sources_mcp_call_count=75,
               prompt_file=str(prompt), prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest())
    raw = (tasks / "review-one.result").read_bytes()

    def no_network(*args, **kwargs):
        pytest.fail("Unsupported review reached the publication/network boundary")

    monkeypatch.setattr(recorder, "_pr", no_network)
    with pytest.raises(recorder.RecordError, match="UKRAINIAN_CLAIMS_UNSUPPORTED") as refused:
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    audit = json.loads(str(refused.value).split(": ", 1)[1])
    assert audit[0]["item_id"] == audit_id
    assert audit[0]["claim"] == {audit_field: audit_claim}
    assert audit[0]["reason"] == "claim_not_in_captured_output"
    assert audit[0]["captured_event_ids"] == ["step-4"]
    assert audit[0]["uncaptured_event_ids"] == (["step-4"] if output is None else [])
    if reply_format == "nested-control":
        assert {next(iter(entry["claim"])) for entry in audit} == {
            "notes.category", "notes.outcome", "notes.id", "notes.location.path", "notes.receipt_id",
        }
    assert comments == [] and calls == {"posts": 0, "statuses": 0}
    assert (tasks / "review-one.result").read_bytes() == raw


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
@pytest.mark.parametrize("shape", [
    "codex_exec_completed", "codex_exec_failed",
    "claude_ok", "claude_is_error",
    "grok_messages_is_error", "grok_stream_completed", "grok_stream_failed", "grok_stream_in_progress",
    "cursor_tool_result_is_error",
    "gemini_success", "gemini_error", "gemini_cancelled",
    "agy_done", "agy_done_with_error", "agy_mcp_is_error_text", "agy_output_too_deep", "agy_output_long_number",
    "codex_rollout_ok", "codex_rollout_structured_ok", "codex_rollout_deep_arguments_ok",
    "codex_rollout_ok_is_error", "codex_rollout_err", "codex_rollout_err_then_output",
])
def test_only_completed_sources_calls_support_unbound_claims(monkeypatch, tmp_path, shape):
    """Defect: a failed or incomplete Sources call's error text supported a claim (C-6314).

    Each provider keeps a failed call's error text where a successful call keeps its output: Codex
    ``status``/``error``, Claude and Grok ``is_error``, MCP ``isError``, ACP and Gemini ``status``, AGY
    ``error`` on a DONE step, a Codex rollout ``Err``. One row per provider shape; ``*_ok``,
    ``*_completed``, ``*_success`` and ``agy_done`` rows prove the completed call still supports it, with
    its structured content and with arguments too deep to decode. MCP JSON text too deep or with too long a
    number to decode might be an error result, so it is not captured, and it never aborts the recorder.
    """
    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import render_item_packet
    from tests.helpers.codex_exec_stream import completed_stream, item_started, jsonl, mcp_call

    tasks, comments, calls = setup_record(monkeypatch, tmp_path)
    packet = build_item_packet([{"id": "item-a", "text": "example"}])
    prompt = tmp_path / "brief.md"
    prompt.write_text(render_item_packet(packet))
    claim = "Rule 8 classifies this suffix as soft-group."
    reply = "VERDICT: APPROVE\n```ukrainian-item-judgments\n" + json.dumps({
        "schema": "ukrainian-item-judgments.v1",
        "items": [{"id": "item-a", "verdict": "APPROVE", "finding": claim, "sources": "none"}],
    }) + "\n```\n"
    arguments = {"query": "suffix"}
    tool = "mcp__sources__search_style_guide"
    text = [{"type": "text", "text": claim}]

    def claude(**result):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_1", "name": tool, "input": arguments}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", **result}]}},
        ]

    def grok_stream(status):
        return [{"type": "tool_call", "toolCallId": "call_1", "toolName": tool, "arguments": arguments,
                 "status": status, "output": claim}]

    def gemini(status):
        return [{"type": "gemini", "toolCalls": [
            {"id": "g1", "name": "mcp_sources_search_style_guide", "args": arguments, "status": status,
             "result": text}]}]

    def agy(**info):
        return [{"event": "step_update", "step_update": {
            "state": "DONE", "step_index": 4, "tool_name": "call_mcp_tool",
            "tool_info": {"name": "call_mcp_tool", "parameters": {
                "ServerName": "sources", "ToolName": "search_style_guide", "Arguments": arguments}, **info}}}]

    def rollout(*results, raw_arguments=json.dumps(arguments)):
        call = {"type": "response_item", "payload": {
            "type": "function_call", "name": "search_style_guide", "namespace": "mcp__sources",
            "arguments": raw_arguments, "call_id": "call_9"}}
        return [call, *({"type": "event_msg", "payload": {
            "type": "mcp_tool_call_end", "call_id": "call_9",
            "invocation": {"server": "sources", "tool": "search_style_guide", "arguments": arguments},
            "result": result}} if isinstance(result, dict) else {"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "call_9", "output": result}}
            for result in results)]

    log = tasks / "logs" / "review-one.stdout.log"
    log.parent.mkdir(exist_ok=True)
    if shape.startswith("codex_exec_"):
        # The event pair `codex exec --json` writes for one MCP call, as in a real Ukrainian-profile review trace.
        # Codex marks a tool-reported error `failed` but keeps its text in `result`, with `error` null.
        finished = mcp_call("sources", "search_style_guide", arguments, text=claim, item_id="item_7")
        finished["item"]["status"] = shape.removeprefix("codex_exec_")
        log.write_text(completed_stream(
            item_started("item_7", "mcp_tool_call", server="sources", tool="search_style_guide", arguments=arguments,
                         result=None, error=None, status="in_progress"),
            finished,
        ))
    else:
        events = {
            "claude_ok": lambda: claude(content=claim, is_error=False),
            "claude_is_error": lambda: claude(content=claim, is_error=True),
            "grok_messages_is_error": lambda: claude(content=text, is_error=True),
            "grok_stream_completed": lambda: grok_stream("completed"),
            "grok_stream_failed": lambda: grok_stream("failed"),
            "grok_stream_in_progress": lambda: grok_stream("in_progress"),
            "cursor_tool_result_is_error": lambda: [
                {"type": "tool_use", "id": "c1", "name": tool, "arguments": arguments},
                {"type": "tool_result", "tool_use_id": "c1", "output": claim, "is_error": True},
            ],
            "gemini_success": lambda: gemini("success"),
            "gemini_error": lambda: gemini("error"),
            "gemini_cancelled": lambda: gemini("cancelled"),
            "agy_done": lambda: agy(output=claim),
            "agy_done_with_error": lambda: agy(output=claim, error={"message": "tool failed"}),
            "agy_mcp_is_error_text": lambda: agy(output=json.dumps({"content": text, "isError": True})),
            "agy_output_too_deep": lambda: agy(output=json.dumps({"content": text, "isError": True})[:-1]
                                               + ', "x": ' + "[" * 513 + "]" * 513 + "}"),
            "agy_output_long_number": lambda: agy(output=json.dumps({"content": text, "isError": True})[:-1]
                                                  + ', "x": 1' + "0" * 5000 + "}"),
            "codex_rollout_ok": lambda: rollout({"Ok": {"content": text, "isError": False}}),
            "codex_rollout_structured_ok": lambda: rollout(
                {"Ok": {"content": [], "structuredContent": {"entry": claim}, "isError": False}}),
            "codex_rollout_deep_arguments_ok": lambda: rollout(
                {"Ok": {"content": text, "isError": False}}, raw_arguments="[" * 513 + "]" * 513),
            "codex_rollout_ok_is_error": lambda: rollout({"Ok": {"content": text, "isError": True}}),
            "codex_rollout_err": lambda: rollout({"Err": claim}),
            "codex_rollout_err_then_output": lambda: rollout({"Err": "tool failed"}, "err: " + claim),
        }[shape]()
        log.write_text(jsonl(*events))
    write_task(tasks, reply=reply, review_profile="ukrainian", sources_mcp_call_count=1,
               prompt_file=str(prompt), prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest())
    if not shape.endswith(("_ok", "_completed", "_success", "_done")):
        with pytest.raises(recorder.RecordError, match="UKRAINIAN_CLAIMS_UNSUPPORTED"):
            recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
        assert comments == [] and calls == {"posts": 0, "statuses": 0}
        return
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED"
    assert len(comments) == 1 and calls == {"posts": 1, "statuses": 1}


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_mixed_family_branch_resolves_one_seat_per_family(tmp_path, monkeypatch):
    """Defect: a mixed-family branch has no eligible reviewer without a third family."""
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, REPOSITORY, SOL, mini_repo
    from tests.agent_runtime.test_review_closeout_cli import _run_cli

    repo = mini_repo(tmp_path, monkeypatch)
    anthropic = repo.commit(OPUS, message="Anthropic change")
    openai = repo.commit(SOL, path="scripts/review/example.py", text="value = 2\n", message="OpenAI change")
    state = tmp_path / "state.json"
    snapshot = tmp_path / "routing.json"
    snapshot.write_text('{"cursor": "unhealthy"}')

    def run(*args):
        return _run_cli(state, *args)

    target = run("target", "--mode", "branch", "--branch", "feature", "--base", "origin/main",
                 "--repo-root", str(repo.root))
    assert target.returncode == 0, target.stderr
    args = ("resolve-reviewer", "--author-model", CURRENT_SOL, "--repository", REPOSITORY,
            "--task-root", str(tmp_path / "tasks"), "--risk", "low", "--domain", "infra",
            "--review-profile", "infra", "--routing-snapshot-file", str(snapshot))
    resolved = run(*args)
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    payload = json.loads(resolved.stdout)
    assert payload["selected"] is None and payload["resolved_risk"] == "critical"
    assert len(payload["quorum"]) == 2
    seats = {seat["family"]: seat for seat in payload["quorum"]}
    assert set(seats) == {"anthropic", "openai"}
    assert {seat["concrete_model"] for seat in seats.values()} == {"claude-opus-5-5", CURRENT_SOL}
    assert seats["anthropic"]["scope"] == {
        "reviewer_family": "anthropic", "author_family": "openai", "commits": [openai], "context_head": openai,
    }
    assert seats["openai"]["scope"] == {
        "reviewer_family": "openai", "author_family": "anthropic", "commits": [anthropic], "context_head": openai,
    }
    assert json.loads(state.read_text())["resolved_reviewer"] == payload
    excluded = run(*args, "--subject-family", "openai")
    assert excluded.returncode == 1 and not json.loads(excluded.stdout)["quorum"]


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_mixed_family_head_needs_both_family_approvals(tmp_path, monkeypatch):
    """Defect: one family's APPROVE lands a mixed-family head."""
    from dataclasses import replace

    from lu_machinery.publish import github
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, SOL, mini_repo
    from tests.review.record_support import REPOSITORY

    repo = mini_repo(tmp_path, monkeypatch)
    anthropic = repo.commit(OPUS, message="Anthropic change")
    head = repo.commit(SOL, message="OpenAI change")
    tasks, comments, calls = setup_record(monkeypatch, tmp_path, head=head)
    facts = recorder.collect_branch_review_facts(
        repository=REPOSITORY, repo_root=repo.root, base_tip_sha=repo.sha("origin/main"), head_sha=head, task_root=tasks,
    )
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_a, **_k: facts)
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields["state"]))
    monkeypatch.setattr(github.gate, "catalog", lambda: {"unit": {"github": REPOSITORY, "default": True, "verdict_recorder": True}})

    def reads(operation, **_kwargs):
        value = {"comments": [comments], "pr-files": [[{"filename": "docs/a.md"}]], "identity": {"login": "fleet"}}
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(value[operation]), stderr="")

    monkeypatch.setattr(github, "read", reads)
    row = {"headRefOid": head, "isDraft": False, "title": "Scoped review", "labels": [], "mergeStateStatus": "CLEAN"}
    write_task(tasks, worktree_base_sha=head, review_risk="critical", reply=attested_approval(),
               result_sha256=hashlib.sha256(attested_approval().encode()).hexdigest())
    with monkeypatch.context() as publication:
        def withhold_reply(_destination, texts, **_kwargs):
            if any("<details>" in text for text in texts):
                blocked = recorder.PublishBlocked("synthetic reply finding")
                blocked.indices = frozenset({0})
                raise blocked

        publication.setattr(recorder, "check_texts", withhold_reply)
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert statuses == ["pending"]
    assert sweep.lookup_verdict(comments, head, "fleet").state == sweep.NEEDS_SEAT
    partial = sweep.lookup_verdict(comments, head, "fleet")
    assert keeper._reason(row, partial, "ok", 0, True) == "needs-CF"
    assert keeper._revoke_reason(row, partial, "ok", True, True, queued=True) == "needs-CF"
    with pytest.raises(github.gate.PublishBlocked):
        github._refuse_incomplete_seats(REPOSITORY, 42, head, None, tmp_path, {})
    marker = sweep.parse_marker(comments[0]["body"])
    assert marker["review_pair"] == "anthropic_openai" and marker["scope_commits"] == (anthropic,)
    assert "Findings are held privately." in comments[0]["body"]
    assert recorder.republish_withheld(
        "review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks",
    )["comment"] == "posted"
    assert sweep.parse_marker(comments[1]["body"]) == marker
    assert statuses == ["pending"] and sweep.lookup_verdict(comments, head, "fleet").state == sweep.NEEDS_SEAT
    write_task(tasks, task_id="review-two", agent="claude", model="claude-opus-5-5",
               worktree_base_sha=head, review_risk="critical", reply=attested_approval())
    recorder.record("review-two", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert statuses == ["pending", "success"] and calls["posts"] == 3
    assert sweep.lookup_verdict(comments, head, "fleet").state == "APPROVED"
    assert keeper._reason(row, sweep.lookup_verdict(comments, head, "fleet"), "ok", 0, True) == "ready"
    github._refuse_incomplete_seats(REPOSITORY, 42, head, None, tmp_path, {})
    assert recorder.require_review_round_within_budget(comments, head, "fleet", budget=1, pr=42) == {}
    moved = "d" * 40
    with pytest.raises(recorder.RecordError, match=r"1 heads.*budget is 1 rounds"):
        recorder.require_review_round_within_budget(comments, moved, "fleet", budget=1, pr=42)
    assert sweep.lookup_verdict(comments, moved, "fleet").state == "CF-stale"
    assert keeper._reason({**row, "headRefOid": moved}, sweep.lookup_verdict(comments, moved, "fleet"), "ok", 0, True) == "needs-CF"
    with pytest.raises(github.gate.PublishBlocked):
        github._refuse_incomplete_seats(REPOSITORY, 42, moved, None, tmp_path, {})

    approved = list(comments)
    for agent, model in (("claude", "claude-opus-5-5"), ("codex", CURRENT_SOL)):
        comments[:] = approved
        write_task(tasks, task_id="review-reject", agent=agent, model=model, worktree_base_sha=head,
                   review_risk="critical", reply="VERDICT: REQUEST_CHANGES\nA blocking defect remains.")
        recorder.record("review-reject", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
        assert sweep.lookup_verdict(comments, head, "fleet").state == "CHANGES_REQUESTED"
        assert statuses[-1] == "failure"
        assert keeper._reason(row, sweep.lookup_verdict(comments, head, "fleet"), "ok", 0, True) == "CF-changes_requested"
        with pytest.raises(github.gate.PublishBlocked):
            github._refuse_incomplete_seats(REPOSITORY, 42, head, None, tmp_path, {})
        with pytest.raises(recorder.RecordError, match="APPROVE_REFUSED"):
            recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")

    facts = replace(facts, commits=tuple(
        replace(commit, task_id="review-one") if commit.family == "anthropic" else commit for commit in facts.commits
    ))
    with pytest.raises(recorder.RecordError, match="SELF_REVIEW_DETECTED"):
        recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    from lu_machinery.agent_runtime.target_admission import ReviewAdmissionRefused
    from lu_machinery.agent_runtime.target_admission import resolve_and_admit
    from tests.claude_route_accounts import HOST_ACCOUNT

    with pytest.raises(ReviewAdmissionRefused, match="SELF_REVIEW_DETECTED"):
        resolve_and_admit(
            ("codex",), model=CURRENT_SOL, mode="read-only", review_dispatch=True,
            review_risk="critical", review_task_id="review-one", review_facts=facts, claude_account=HOST_ACCOUNT,
        )
    monkeypatch.setattr(recorder, "_pr", lambda *_a, **_k: {"number": 42, "headRefName": "codex/42", "headRefOid": moved})
    with pytest.raises(recorder.RecordError, match="head moved"):
        recorder.record("review-two", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")


NEON = "agy/gemini-4-neon-high"


def _branch_facts(repo, head, tasks):
    from tests.agent_runtime.test_authoring_review_feasibility import REPOSITORY

    return recorder.collect_branch_review_facts(
        repository=REPOSITORY, repo_root=repo.root, base_tip_sha=repo.sha("origin/main"), head_sha=head, task_root=tasks,
    )


def _resolve(facts):
    from lu_machinery.review.reviewer_resolver import resolve_reviewer
    from tests.claude_route_accounts import HOST_ACCOUNT

    return resolve_reviewer(
        facts.resolver_inputs(risk="critical", review_profile="infra", claude_account=HOST_ACCOUNT, structural=True)
    )


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_three_family_branch_resolves_a_pair_covering_every_commit(tmp_path, monkeypatch):
    """Defect: a branch authored by all three critical-tier families has no eligible reviewer."""
    from dataclasses import replace

    from lu_machinery.review.app_bot_authorship import VERIFIED_APP_BOT
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, SOL, mini_repo

    repo = mini_repo(tmp_path, monkeypatch)
    by_family = {
        "openai": repo.commit(SOL, message="OpenAI change"),
        "anthropic": repo.commit(OPUS, message="Anthropic change"),
        "google": repo.commit(NEON, message="Google change"),
    }
    facts = _branch_facts(repo, by_family["google"], tmp_path / "tasks")
    assert facts.mixed_family
    result = _resolve(facts)
    assert result.selected is None and result.fail_closed_reason is None
    seats = {seat.family for seat in result.quorum}
    assert len(result.quorum) == 2 and len(seats) == 2 and seats <= set(by_family)
    scopes = {scope.reviewer_family: scope for scope in result.review_scopes}
    assert set(scopes) == seats
    for reviewer, scope in scopes.items():
        assert set(scope.commits) == {sha for family, sha in by_family.items() if family != reviewer}
        assert scope.author_family == ",".join(sorted(set(by_family) - {reviewer}))
        assert {commit.sha for commit in facts.reviewer_scope(reviewer).commits} == set(scope.commits)
    assert set().union(*(scope.commits for scope in scopes.values())) == set(by_family.values())
    # No three-family seat is scoped to a verified bot commit, so such a branch fails closed.
    bot = recorder.CommitAttribution("f" * 40, VERIFIED_APP_BOT, VERIFIED_APP_BOT, authorship=VERIFIED_APP_BOT)
    with_bot = _resolve(replace(facts, commits=(*facts.commits, bot)))
    assert not with_bot.quorum and "verified app-bot commit" in with_bot.fail_closed_reason


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_three_family_head_needs_an_outside_approval_for_every_family(tmp_path, monkeypatch):
    """Defect: a three-family head lands while one family's commits have no outside-family approval."""
    from lu_machinery.publish import github
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, SOL, mini_repo
    from tests.review.record_support import REPOSITORY

    repo = mini_repo(tmp_path, monkeypatch)
    anthropic = repo.commit(OPUS, message="Anthropic change")
    openai = repo.commit(SOL, message="OpenAI change")
    head = repo.commit(NEON, message="Google change")
    tasks, comments, _calls = setup_record(monkeypatch, tmp_path, head=head)
    facts = _branch_facts(repo, head, tasks)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_a, **_k: facts)
    statuses = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields["state"]))
    monkeypatch.setattr(github.gate, "catalog", lambda: {"unit": {"github": REPOSITORY, "default": True, "verdict_recorder": True}})

    def reads(operation, **_kwargs):
        value = {"comments": [comments], "pr-files": [[{"filename": "docs/a.md"}]], "identity": {"login": "fleet"}}
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(value[operation]), stderr="")

    monkeypatch.setattr(github, "read", reads)
    write_task(tasks, worktree_base_sha=head, review_risk="critical", reply=attested_approval())
    recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    marker = sweep.parse_marker(comments[0]["body"])
    assert marker["review_pair"] == "anthropic_google_openai"
    assert marker["scope_commits"] == tuple(sorted((anthropic, head)))
    # The OpenAI seat cannot cover OpenAI's own commit, so the gate stays shut.
    assert statuses == ["pending"] and sweep.lookup_verdict(comments, head, "fleet").state == sweep.NEEDS_SEAT
    with pytest.raises(github.gate.PublishBlocked):
        github._refuse_incomplete_seats(REPOSITORY, 42, head, None, tmp_path, {})
    write_task(tasks, task_id="review-two", agent="claude", model="claude-opus-5-5",
               worktree_base_sha=head, review_risk="critical", reply=attested_approval())
    recorder.record("review-two", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    scoped = ",".join(sorted((openai, head)))
    assert sweep.parse_marker(comments[1]["body"])["scope_commits"] == tuple(sorted((openai, head)))
    assert statuses == ["pending", "success"] and sweep.lookup_verdict(comments, head, "fleet").state == "APPROVED"
    github._refuse_incomplete_seats(REPOSITORY, 42, head, None, tmp_path, {})
    # A second record whose scope drops OpenAI's commit, or a relabelled copy of the OpenAI record,
    # leaves OpenAI's commit without an outside-family approval.
    forged = [
        {**comments[1], "id": 90, "body": comments[1]["body"].replace(scoped, head)},
        {**comments[0], "id": 91, "body": comments[0]["body"].replace("family=openai", "family=anthropic")
         .replace("Reviewer family: openai", "Reviewer family: anthropic")
         .replace("Scope author family: anthropic,google", "Scope author family: google,openai")},
    ]
    assert sweep.parse_marker(forged[1]["body"]) is None
    for extra in forged:
        assert sweep.lookup_verdict([comments[0], extra], head, "fleet").state != "APPROVED"


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_anthropic_openai_pair_resolves_as_before(tmp_path, monkeypatch):
    """Control: widening the pair to the critical tier changes the Anthropic/OpenAI pair or admits Neon to it."""
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, SOL, mini_repo

    repo = mini_repo(tmp_path, monkeypatch)
    anthropic = repo.commit(OPUS, message="Anthropic change")
    openai = repo.commit(SOL, message="OpenAI change")
    facts = _branch_facts(repo, openai, tmp_path / "tasks")
    result = _resolve(facts)
    assert {seat.family for seat in result.quorum} == {"anthropic", "openai"}
    assert {scope.reviewer_family: (scope.author_family, scope.commits) for scope in result.review_scopes} == {
        "anthropic": ("openai", (openai,)), "openai": ("anthropic", (anthropic,)),
    }
    neon = [entry for entry in result.trace if entry.family == "google"]
    assert neon and all(entry.status == "excluded" for entry in neon)
    with pytest.raises(recorder.RecordError, match="requires an Anthropic or OpenAI seat"):
        facts.reviewer_scope("google")


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_single_google_branch_still_excludes_google(tmp_path, monkeypatch):
    """Control: the three-family pair admits a Google seat to a Google-only branch, or pairs Anthropic/Google."""
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, mini_repo

    repo = mini_repo(tmp_path, monkeypatch)
    google = repo.commit(NEON, message="Google change")
    for head, authors in ((google, {"google"}), (repo.commit(OPUS, message="Anthropic change"), {"anthropic", "google"})):
        facts = _branch_facts(repo, head, tmp_path / "tasks")
        assert facts.existing_families == authors and not facts.mixed_family
        result = _resolve(facts)
        assert result.selected is not None and result.selected.family not in authors
        assert not result.quorum and not result.review_scopes


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_single_family_branch_resolution_unchanged(tmp_path, monkeypatch):
    """Control: the pair rule changes single-family routing."""
    from dataclasses import asdict, replace

    from lu_machinery.review.reviewer_resolver import resolve_reviewer
    from tests.claude_route_accounts import HOST_ACCOUNT
    from tests.agent_runtime.test_authoring_review_feasibility import OPUS, REPOSITORY, SOL, mini_repo

    for trailer, model, expected in ((OPUS, "claude-opus-5-5", "openai"), (SOL, CURRENT_SOL, "anthropic")):
        directory = tmp_path / expected
        directory.mkdir()
        repo = mini_repo(directory, monkeypatch)
        head = repo.commit(trailer)
        facts = recorder.collect_branch_review_facts(
            repository=REPOSITORY, repo_root=repo.root, base_tip_sha=repo.sha("origin/main"), head_sha=head,
            task_root=directory / "tasks",
        )
        inputs = facts.resolver_inputs(risk="critical", review_profile="infra", claude_account=HOST_ACCOUNT, structural=True)
        result = resolve_reviewer(inputs)
        legacy = resolve_reviewer(replace(inputs, author_model=model, author_families=frozenset(), author_commits=()))
        assert result == legacy and result.selected.family == expected
        assert not result.quorum and not result.review_scopes
        assert "review_scopes" not in asdict(result)


# Dual-slug transfer bridge (A01b): one row baked as org/repos and neworg/repos, bound to repository_id 4242.
_BAKED = ("org/repos", "neworg/repos")


def _transferred(monkeypatch, active):
    """Make the fleet registry one transferred row whose host selector chooses *active*."""
    from lu_machinery.common import git_credentials
    from lu_machinery.orchestration import fleet_repos

    row = fleet_repos.FleetRepo(
        "case", "org/repos", "repo", "public-labs", default=True,
        repository_id=4242, visibility="public", alternate_github="neworg/repos",
    )
    monkeypatch.setattr(fleet_repos, "_repository_catalog", lambda _path: {"case": row})
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda path=None: {"case": row})
    monkeypatch.setattr(recorder, "load_fleet_repos", lambda: {"case": row})
    monkeypatch.setattr(
        git_credentials, "load_selector", lambda **_kwargs: git_credentials.SelectorState({"case": active}, "d" * 64),
    )
    return row


@pytest.mark.parametrize("active", _BAKED)
@pytest.mark.parametrize(("stored", "review", "same"), [
    ("org/repos", "neworg/repos", True), ("neworg/repos", "org/repos", True), ("evil/repo", "neworg/repos", False),
])
def test_author_task_under_either_baked_slug_binds_to_the_review_repository(tmp_path, monkeypatch, active, stored, review, same):
    """Defect class: an author task recorded under one baked slug of a transferred repository is read as another
    repository's task once the review names the other slug, so every review across the transfer refuses."""
    _transferred(monkeypatch, active)
    (tmp_path / "author-one.json").write_text(json.dumps(
        {"task_id": "author-one", "repository": stored, "agent": "claude", "model": "claude-opus-5-5"},
    ))

    def attribute():
        return recorder._attribute_trailer(None, "claude/author-one", repository=review, task_root=tmp_path)

    if same:
        assert attribute().family == "anthropic"
    else:
        with pytest.raises(recorder.RecordError, match="provenance conflicts"):
            attribute()


@pytest.mark.parametrize("active", _BAKED)
@pytest.mark.parametrize(("stored", "review", "same"), [
    ("org/repos", "neworg/repos", True), ("neworg/repos", "org/repos", True), ("evil/repo", "org/repos", False),
])
def test_authoring_admission_under_either_baked_slug_admits_its_history(tmp_path, monkeypatch, active, stored, review, same):
    """Defect class: a red-team authoring admission receipt written under one baked slug is ignored once the
    review names the other slug, so its admitted unknown history is lost across the transfer."""
    _transferred(monkeypatch, active)
    history, admitted, remote = "1" * 40, "2" * 40, "3" * 40
    (tmp_path / "admit-one.json").write_text(json.dumps({"task_id": "admit-one", "status": "done",
        "authoring_review_admission": {
            "repository": stored, "head_branch": "codex/topic", "review_plan": "red_team",
            "unknown_history": [history], "head_sha": admitted, "history_remote_head_sha": remote,
            "reviewer": {"review_mode": "red_team"}, "check": "structural", "applicable": True,
            "assumed_model": None, "refusal": None,
        }}))
    ancestors = {SHA: [admitted, remote, history], admitted: [remote, history], remote: [history]}
    monkeypatch.setattr(recorder, "_facts_git", lambda _root, args, **_kwargs: "\n".join(ancestors[args[1]]).encode())
    assert recorder.admitted_unknown_history(
        repository=review, branch="codex/topic", head_sha=SHA, task_root=tmp_path, repo_root=tmp_path,
    ) == ((history,) if same else ())


@pytest.mark.parametrize("active", _BAKED)
@pytest.mark.parametrize("spelling", [*_BAKED, "evil/repo", "org/repo\u017f"])
def test_review_checkout_resolves_either_baked_slug_and_refuses_another(tmp_path, monkeypatch, active, spelling):
    """Defect class: the review checkout lookup matches only the registry's first slug, so a review named by the
    transferred repository's other slug is reported as unregistered."""
    row = _transferred(monkeypatch, active)
    monkeypatch.setattr(recorder, "resolve_review_repository", lambda root, key, **_kwargs: root / key)
    if spelling in _BAKED:
        assert recorder._review_repository_root(spelling, tmp_path) == tmp_path / "case"
        # The review budget policy keys either baked slug to the row's one registry slug.
        assert recorder._budget_key(spelling, {"case": row}) == "github.com/org/repos"
        return
    with pytest.raises(recorder.RecordError, match="not uniquely registered"):
        recorder._review_repository_root(spelling, tmp_path)
    # Another repository keeps its own key; a non-ASCII look-alike is no repository at all.
    assert recorder._budget_key(spelling, {"case": row}) == ("unknown" if not spelling.isascii() else "github.com/evil/repo")


@pytest.mark.usefixtures("configured_roots")
@pytest.mark.parametrize("active", _BAKED)
@pytest.mark.parametrize("spelling", [*_BAKED, "evil/repo"])
def test_publication_import_reads_the_active_slug_and_stores_the_registry_slug(tmp_path, monkeypatch, active, spelling):
    """Defect class: the publication import reads a transferred repository at whichever slug it was given and
    stores that spelling, so the ledger loses its join to the model-statistics rows; a wrong slug is refused."""
    from tests.review.record_support import recorded_verdict

    _transferred(monkeypatch, active)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    read = []

    class Adapter:
        def identity(self):
            return "fleet"

        def comments(self, repository, number):
            read.append(repository)
            return [recorded_verdict(SHA, "APPROVED")]

    def run():
        return recorder.import_publications(days=1, dry_run=False, task_root=tasks, adapter=Adapter(), only=(spelling, 42))

    if spelling == "evil/repo":
        with pytest.raises(recorder.RecordError, match="publication repository unresolved"):
            run()
        assert read == []
        return
    assert run()["added"] == 1
    assert read == [active]
    assert [(entry["repository"], entry["fleet_repo_selector"]) for entry in _ledger(tmp_path)] == [
        ("org/repos", {"selector_value": active, "selector_sha256": "d" * 64}),
    ]


@pytest.mark.usefixtures('configured_roots', 'recorder_matcher')
@pytest.mark.usefixtures("agent_runtime_roots")
@pytest.mark.parametrize("active", _BAKED)
def test_recorded_verdict_posts_at_the_active_slug_and_names_the_selector(monkeypatch, tmp_path, active):
    """Defect class: after a transfer the recorder writes the verdict and status to the slug the review task was
    dispatched with, not the selector's active slug, and its receipt never names the selector it relied on."""
    tasks, _comments, calls = setup_record(monkeypatch, tmp_path)
    _transferred(monkeypatch, active)
    write_task(tasks, reply="VERDICT: APPROVE\nOrdinary reviewer text.\n", repository="org/repos")
    written = []
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: written.append(fields["repository"]))
    post = recorder._post_verdict
    monkeypatch.setattr(recorder, "_post_verdict", lambda repository, *args: (written.append(repository), post(repository, *args))[1])
    facts, reads = recorder.pr_review_facts, []
    monkeypatch.setattr(recorder, "pr_review_facts", lambda repository, number, **kwargs: (
        reads.append((repository, kwargs.get("github_repository"))), facts(repository, number, **kwargs))[1])
    result = recorder.record("review-one", pr_number=42, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["comment"] == "posted" and calls["posts"] == 1
    assert written == [active, active]
    assert reads == [("org/repos", active)]  # records matched at the registry slug, GitHub read at the active one
    assert result["fleet_repo_selector"] == {"selector_value": active, "selector_sha256": "d" * 64}
    assert [(entry["repository"], entry["fleet_repo_selector"]) for entry in _ledger(tmp_path)] == [
        ("org/repos", result["fleet_repo_selector"]),
    ]


@pytest.mark.parametrize("active", _BAKED)
def test_publication_ledger_keeps_the_selector_receipt_and_still_reads_legacy_lines(tmp_path, monkeypatch, active):
    """Defect class: the publication ledger drops the selector receipt a dual-slug publication was resolved with
    (SELECTOR-DURABLE-RECEIPTS), rejects a line written before the receipt existed, or stores or trusts a malformed
    receipt; or the receipt changes which lines are one publication."""
    from collections import Counter, UserString
    from types import MappingProxyType

    from lu_machinery.api import model_stats_rows
    from tests.review.record_support import recorded_verdict

    class ReceiptText(str):
        def __str__(self):
            return "overridden"

    _transferred(monkeypatch, active)
    receipt = {"selector_value": active, "selector_sha256": "d" * 64}
    supplied = {ReceiptText(key): ReceiptText(value) for key, value in receipt.items()}
    comment = recorded_verdict(SHA, "APPROVED")
    entry, reason = recorder.publication_of("org/repos", 42, comment, fleet_repo_selector=supplied)
    assert reason == "" and entry is not None and entry["fleet_repo_selector"] == receipt
    assert all(type(key) is str and type(value) is str for key, value in entry["fleet_repo_selector"].items())
    legacy, _ = recorder.publication_of("org/repos", 41, comment)
    assert legacy is not None and "fleet_repo_selector" not in legacy
    for bad in ({"selector_value": active}, {**receipt, "extra": "x"}, {**receipt, "selector_sha256": "D" * 64},
                {**receipt, "selector_value": "../x"}, {**receipt, "selector_value": "org/.."},
                {**receipt, "selector_value": None}):
        assert recorder.publication_of("org/repos", 42, comment, fleet_repo_selector=bad) == (None, "malformed field")
    # A malformed receipt in the second entry must leave the whole batch unwritten.
    class DuplicateReceiptKeys(dict):
        def items(self):
            return [("selector_value", "invalid"), *super().items()]

    for invalid_receipt in (
        MappingProxyType(receipt),
        {UserString(key): value for key, value in receipt.items()},
        DuplicateReceiptKeys(receipt),
    ):
        with pytest.raises(ValueError):
            model_stats_rows.record_publications(tmp_path, [legacy, {**entry, "fleet_repo_selector": invalid_receipt}])
    assert model_stats_rows.read_publications(tmp_path)[0] == {}
    assert model_stats_rows.record_publications(tmp_path, [legacy, entry]) == 2
    # The receipt is no part of the publication key: the same publication without it is a duplicate.
    assert model_stats_rows.record_publications(tmp_path, [{k: v for k, v in entry.items() if k != "fleet_repo_selector"}]) == 0
    with (tmp_path / model_stats_rows.ROWS_DIR / model_stats_rows.PUBLICATIONS_FILE).open("ab") as stream:
        stream.write((json.dumps({**entry, "pr": 43, "fleet_repo_selector": {"selector_value": active}}) + "\n").encode())
    published, counts = model_stats_rows.read_publications(tmp_path)
    assert sorted(published["prior-review"], key=lambda line: line["pr"]) == [legacy, entry]
    assert counts == Counter({"lines": 2, "malformed_lines": 1, "tasks": 1})

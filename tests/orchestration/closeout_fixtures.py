"""Verbatim helpers from LU tests/orchestration/test_closeout_run_evidence.py at 5aba2adb.

Only the existing package-3 import codemod was applied before extraction.
"""

from __future__ import annotations

import pytest

from lu_machinery.orchestration import lifecycle_observation as lo
from lu_machinery.orchestration import task_lifecycle as tl
from tests.orchestration.test_task_lifecycle import HEAD, MERGE, NOW, REVIEW_URL, _identity, _observation

RUN_URL = "https://github.com/org/repo/actions/runs/123"


JOB_URL = RUN_URL + "/job/456"


BODY = "- [x] **AC-RUN** — The criterion has executed tests and CI.\n"


def evidence(ledger, kind, *, ac_id="AC-RUN", url=JOB_URL, details=None):
    return tl.add_evidence(
        ledger, ac_id=ac_id, evidence_type=kind, summary="criterion evidence",
        url=url, commit=HEAD, details=details or {}, recorded_at=NOW,
    )[0]


@pytest.fixture
def bound_case():
    snapshot = tl.build_ac_snapshot(BODY, {
        "AC-RUN": {"due_state": "MERGED", "required_evidence": ["test", "ci", "review"]},
    }, finalized_at=NOW)
    ledger = tl.build_lifecycle(_identity(), author_family="codex", ac_snapshot=snapshot,
                                required_checks=["CI Gate"], now=NOW, pr_number=77)
    ledger = evidence(ledger, "test", details={"step_names": ["Run criterion tests"]})
    ledger = evidence(ledger, "review", url=REVIEW_URL, details={
        "author_family": "codex", "reviewer_family": "claude", "verdict": "pass",
    })
    ledger, _, _ = tl.reconcile(ledger, _observation(BODY), now=NOW)
    pull = {
        "number": 77, "html_url": "https://github.com/org/repo/pull/77", "merged": True,
        "merged_at": NOW, "merge_commit_sha": MERGE, "state": "closed", "head": {"sha": HEAD},
        "base": {"ref": "main", "repo": {"full_name": "org/repo"}},
        "created_at": "2026-07-16T08:00:00Z",
    }
    run = {
        "id": 123, "run_attempt": 2, "repository": {"full_name": "org/repo"},
        "event": "merge_group", "head_sha": MERGE, "head_branch": "gh-readonly-queue/main/pr-77-abcdef",
        "html_url": RUN_URL, "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success",
        "created_at": "2026-07-16T09:00:00Z", "run_started_at": "2026-07-16T09:01:00Z",
    }
    step = {"name": "Run criterion tests", "status": "completed", "conclusion": "success",
            "started_at": "2026-07-16T09:02:00Z", "completed_at": "2026-07-16T09:03:00Z"}
    job = {"id": 456, "run_id": 123, "run_attempt": 2, "head_sha": MERGE, "html_url": JOB_URL,
           "name": "CI Gate", "status": "completed", "conclusion": "success",
           "started_at": "2026-07-16T09:01:00Z", "completed_at": "2026-07-16T09:05:00Z", "steps": [step]}
    return ledger, _observation(BODY, pr_state="MERGED", clean=True), pull, run, [job]


def derive(case):
    ledger, observation, pull, run, jobs = case
    return lo.merged_run_evidence(ledger, observation, pull=pull, run=run, jobs=jobs, now=NOW)

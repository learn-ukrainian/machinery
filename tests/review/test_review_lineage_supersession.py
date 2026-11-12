"""An unpublished verdict that a later verdict of its own seat superseded (``review_lineage.superseded_runs``)."""

from __future__ import annotations

import pytest

from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review.review_lineage import LineageRefused, ReviewRun, check_lineage, superseded_runs

EARLY = "1" * 40
APPROVED = "2" * 40
CANDIDATE = "3" * 40
SEATS = {"openai": ("gpt-6.1-sol", "codex"), "anthropic": ("claude-opus-5-5", "claude")}


def started(minute):
    return f"2026-10-28T20:{minute:02d}:00.000001+00:00"


def review(task, sha, reply, *, minute, family="openai", profile="code", status="done", publish=True, **marker):
    """A review run and, when ``publish``, the verdict comment the recorder would post for it."""
    model, agent = SEATS[family]
    record = {
        "task_id": task,
        "worktree_base_sha": sha,
        "pinned_head": sha,
        "model": model,
        "agent": agent,
        "started_at": started(minute),
        "status": status,
        "review_profile": profile,
    }
    if profile == "ukrainian":
        record["sources_mcp_call_count"] = 3
    run = ReviewRun(task, record, reply)
    if not publish:
        return run, None
    fields = dict(
        sha=sha,
        task_id=task,
        started=started(minute),
        verdict=recorder.normalize_verdict(reply),
        model=model,
        family=family,
        reply=reply,
        review_profile=profile,
        sources_mcp_call_count=3 if profile == "ukrainian" else None,
    )
    fields.update(marker)
    stamp = f"2026-10-28T21:{minute:02d}:00Z"
    comment = {
        "id": minute,
        "body": recorder.build_comment(**fields),
        "user": {"login": "fleet"},
        "author_association": "MEMBER",
        "created_at": stamp,
        "updated_at": stamp,
    }
    return run, comment


def lineage(*pairs, approved=APPROVED, exclude_head=None):
    runs = [run for run, _ in pairs]
    comments = sorted((comment for _, comment in pairs if comment), key=lambda item: item["created_at"])
    seats = check_lineage(runs=runs, comments=comments, login="fleet", approved=approved, exclude_head=exclude_head, second_review=False)
    return seats, superseded_runs(runs=runs, comments=comments, login="fleet", exclude_head=exclude_head)


def report(*pairs, exclude_head=None):
    comments = sorted((comment for _, comment in pairs if comment), key=lambda item: item["created_at"])
    return superseded_runs(runs=[run for run, _ in pairs], comments=comments, login="fleet", exclude_head=exclude_head)


ORPHAN_REJECTION = "VERDICT: REQUEST_CHANGES\nF1 [P1] scripts/unit.py:1 the guard drops a case."


def test_a_later_same_seat_approve_supersedes_an_unpublished_rejection_and_keeps_it_listed():
    orphan = review("sol-r1", EARLY, ORPHAN_REJECTION, minute=1, publish=False)
    witness = review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)
    with pytest.raises(LineageRefused, match="review task sol-r1 finished but its verdict is unpublished"):
        lineage(orphan)
    seats, superseded = lineage(orphan, witness)
    assert seats == ["openai"]
    assert superseded == [
        {
            "task": "sol-r1",
            "started": started(1),
            "head": EARLY,
            "verdict": "CHANGES_REQUESTED",
            "findings": recorder._review_verdict_and_count(ORPHAN_REJECTION)[1],
            "model": "gpt-6.1-sol",
            "family": "openai",
            "profile": "code",
            "superseded_by": {
                "task": "sol-r2",
                "started": started(2),
                "head": APPROVED,
                "verdict": "APPROVED",
                "model": "gpt-6.1-sol",
            },
        }
    ]


@pytest.mark.parametrize(
    "seat", [{"family": "anthropic"}, {"profile": "ukrainian"}], ids=["other-family", "other-profile"]
)
def test_another_seats_verdict_supersedes_nothing(seat):
    orphan = review("sol-r1", EARLY, "VERDICT: APPROVE", minute=1, publish=False)
    witness = review("other-r2", APPROVED, "VERDICT: APPROVE", minute=2, **seat)
    assert report(orphan, witness) == []
    with pytest.raises(LineageRefused, match="review task sol-r1 finished but its verdict is unpublished"):
        lineage(orphan, witness)


@pytest.mark.parametrize("orphan_head,orphan_minute", [(APPROVED, 1), (EARLY, 3)], ids=["same-head", "older-witness"])
def test_a_same_head_or_older_witness_supersedes_nothing(orphan_head, orphan_minute):
    witness = review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)
    orphan = review("sol-late", orphan_head, "VERDICT: APPROVE", minute=orphan_minute, publish=False)
    assert report(orphan, witness) == []
    with pytest.raises(LineageRefused, match="review task sol-late finished but its verdict is unpublished"):
        lineage(orphan, witness)


def test_an_approval_at_the_candidate_head_never_supersedes_to_prove_the_approved_head(monkeypatch):
    # Missing Carbon at a High head cannot certify lineage or a clean-base exemption.
    sol_run, sol_comment = review("sol-high", APPROVED, "VERDICT: APPROVE", minute=2,
                                  author_models=["claude-opus-5-5"])
    with pytest.raises(LineageRefused, match="needs-seat"):
        check_lineage(runs=[sol_run], comments=[sol_comment], login="fleet", approved=APPROVED, second_review=True)
    assert not recorder._fully_approved([sol_comment], APPROVED, "fleet")
    assert recorder.budget_exempt_heads(
        [sol_comment], CANDIDATE, "fleet", prove=lambda *_: ["b" * 40],
        approval_context=lambda _: {"second_review": True},
    ) == {}
    monkeypatch.setitem(SEATS, "google", ("gemini-4-carbon-medium", "agy"))
    carbon_run, carbon_comment = review("carbon-old", EARLY, "VERDICT: REQUEST_CHANGES", minute=1,
                                        family="google", second_review=True, author_models=["claude-opus-5-5"])
    exempt_run, exempt_comment = review("sol-exempt", APPROVED, "VERDICT: APPROVE", minute=2,
                                        author_models=["gemini-4-carbon-medium"])
    assert check_lineage(runs=[carbon_run, exempt_run], comments=[carbon_comment, exempt_comment],
                         login="fleet", approved=APPROVED, second_review=True) == ["openai"]
    opus = review("opus-r2", APPROVED, "VERDICT: APPROVE", minute=2, family="anthropic")
    orphan = review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2, publish=False)
    at_candidate = review("sol-r3", CANDIDATE, "VERDICT: APPROVE", minute=3)
    assert report(opus, orphan, at_candidate, exclude_head=CANDIDATE) == []
    with pytest.raises(LineageRefused, match="review task sol-r2 finished but its verdict is unpublished"):
        lineage(opus, orphan, at_candidate, exclude_head=CANDIDATE)


def _edited(pair):
    run, comment = pair
    return run, {**comment, "updated_at": "2026-10-28T23:59:00Z"}


def _unattested(pair):
    run, comment = pair
    return ReviewRun(run.task_id, {**run.record, "model": "unknown-model"}, run.reply), comment


def _pinned(pair, pin):
    run, comment = pair
    return ReviewRun(run.task_id, {**run.record, "pinned_head": pin}, run.reply), comment


UNSUPERSEDED = "review task sol-r1 finished but its verdict is unpublished"


@pytest.mark.parametrize(
    "damage,refusal",
    [
        (
            lambda: review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2, verdict="CHANGES_REQUESTED"),
            "published verdict of sol-r2 conflicts with its task record",
        ),
        (lambda: _edited(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)), "was edited"),
        (
            lambda: _unattested(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)),
            "review task sol-r2 evidence refused",
        ),
        (
            lambda: review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2, status="failed"),
            r"review task sol-r2 \(failed\) carries an unpublished verdict",
        ),
        # A witness pinned to another head, or to no valid head, is evidence the recorder refuses.
        (lambda: _pinned(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2), CANDIDATE), UNSUPERSEDED),
        (lambda: _pinned(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2), "invalid-sha"), UNSUPERSEDED),
        (lambda: _pinned(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2), []), UNSUPERSEDED),
    ],
    ids=[
        "conflicting-marker",
        "edited-marker",
        "unattested-reviewer",
        "unfinished-run",
        "pinned-elsewhere",
        "pinned-invalid",
        "pinned-malformed",
    ],
)
def test_a_malformed_witness_supersedes_nothing_and_the_lineage_still_refuses(damage, refusal):
    orphan = review("sol-r1", EARLY, "VERDICT: APPROVE", minute=1, publish=False)
    witness = damage()
    assert report(witness, orphan) == []
    # The witness is read first, so its own defect is the refusal.
    with pytest.raises(LineageRefused, match=refusal):
        lineage(witness, orphan)


def crashed_without_reply(task, sha, *, minute, provider_pgid=None, result_sha256=None, publish=False):
    """A terminal crashed run. The supervisor pid is set; the reply was never written.

    ``provider_pgid`` and ``result_sha256`` default to null, the shape of a run whose provider never started.
    """
    run, comment = review(task, sha, "VERDICT: APPROVE", minute=minute, status="crashed", publish=publish)
    record = {
        **run.record,
        "provider_pgid": provider_pgid,
        "result_sha256": result_sha256,
        "pid": 1001,
        "process_start": "supervisor:1",
    }
    return ReviewRun(task, record, None), comment


def test_a_crashed_run_that_never_started_a_provider_is_admitted():
    """Defect class: a crashed review with no provider pgid, no result digest and no reply blocks the lineage forever.

    The supervisor pid can be recorded. That run carries no verdict, so a later published APPROVE still stands.
    """
    approve = review("sol-r1", APPROVED, "VERDICT: APPROVE", minute=1)
    crashed = crashed_without_reply("crashed-r0", EARLY, minute=0)
    seats, superseded = lineage(crashed, approve)
    assert seats == ["openai"]
    assert superseded == []


@pytest.mark.parametrize(
    "provider_pgid,result_sha256",
    [(4242, None), (None, "ab" * 32)],
    ids=["provider-pgid", "result-digest"],
)
def test_a_crashed_run_whose_record_does_not_prove_the_provider_never_ran_is_still_refused(
    provider_pgid, result_sha256
):
    """Defect class: a missing reply is admitted when the record shows a provider pgid or a result digest.

    Either fact means a provider may have run, so the missing reply still refuses.
    """
    approve = review("sol-r1", APPROVED, "VERDICT: APPROVE", minute=1)
    crashed = crashed_without_reply(
        "crashed-r0", EARLY, minute=0, provider_pgid=provider_pgid, result_sha256=result_sha256
    )
    with pytest.raises(LineageRefused, match=r"review task crashed-r0 \(crashed\) has no reply on record"):
        lineage(crashed, approve)


def test_a_published_verdict_naming_a_never_started_run_is_refused():
    """Defect class: a published verdict is accepted for a review run that never started a provider.

    A verdict cannot belong to that run, so the lineage refuses it.
    """
    crashed = crashed_without_reply("never-started", APPROVED, minute=1, publish=True)
    with pytest.raises(LineageRefused, match="published verdict of task never-started has no review run on record"):
        lineage(crashed)

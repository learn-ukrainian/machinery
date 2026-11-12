"""Existing exact-head verdict cases ported from the product's same path."""

import pytest

from lu_machinery.orchestration import integration_sweep as sweep
from lu_machinery.review.record_cf_verdict import build_comment

SHA = "a" * 40
START = "2026-09-23T12:00:00.000001+00:00"


def comment(
    *,
    sha=SHA,
    task="review-one",
    started=START,
    verdict="APPROVED",
    login="fleet",
    association="MEMBER",
    edited=False,
    review_mode="cross_family",
    family="openai",
):
    body = build_comment(
        sha=sha,
        task_id=task,
        started=started,
        verdict=verdict,
        model="gpt-6.1-sol",
        family=family,
        reply="VERDICT: APPROVE",
        review_mode=review_mode,
    )
    return {
        "id": task,
        "body": body,
        "user": {"login": login},
        "author_association": association,
        "created_at": "2026-09-23T13:00:00Z",
        "updated_at": "2026-09-23T13:01:00Z" if edited else "2026-09-23T13:00:00Z",
    }


@pytest.mark.parametrize("mode", ["cross_family", "red_team"])
def test_review_mode_round_trip_and_legacy_marker(mode):
    item = comment(review_mode=mode)
    marker = sweep.parse_marker(item["body"])
    assert marker is not None
    assert marker.get("review_mode", "cross_family") == mode
    verdict = sweep.lookup_verdict([item], SHA, "fleet")
    assert verdict.state == "APPROVED"
    assert verdict.review_mode == mode


@pytest.mark.parametrize("mode", ["cross_family", "red_team"])
def test_unknown_reviewer_marker_never_approves(mode):
    assert sweep.lookup_verdict([comment(review_mode=mode, family="unknown")], SHA, "fleet").state == "unknown"


def test_latest_started_rejects_approval_then_rejection():
    rows = [
        comment(),
        comment(task="review-two", started="2026-09-23T12:00:01.000001+00:00", verdict="CHANGES_REQUESTED"),
    ]
    assert sweep.lookup_verdict(rows, SHA, "fleet").state == "CHANGES_REQUESTED"


@pytest.mark.parametrize("bad", ["edited", "malformed"])
def test_edited_or_unparseable_marker_makes_sha_unknown(bad):
    item = comment(edited=bad == "edited")
    if bad == "malformed":
        item["body"] = item["body"].replace("verdict=APPROVED", "verdict=MAYBE")
    assert sweep.lookup_verdict([item], SHA, "fleet").state == "unknown"


# Binding second review (operator 2026-11-11; ordered list Muse, Carbon, Sol, Opus since
# 2026-11-12): High-tier and security-sensitive PRs.
CARBON = "gemini-4-carbon-medium"
MUSE = "muse-wonder-full-1.0"


def record(*, task, model="gpt-6.1-sol", family="openai", verdict="APPROVED", second=False,
           authors=("claude-opus-5-5",), started=START, reply="VERDICT: APPROVE", **extra):
    body = build_comment(
        sha=SHA, task_id=task, started=started, verdict=verdict, model=model, family=family,
        reply=reply, author_models=authors, second_review=second, **extra,
    )
    return {
        "id": task, "body": body, "user": {"login": "fleet"}, "author_association": "MEMBER",
        "created_at": "2026-09-23T13:00:00Z", "updated_at": "2026-09-23T13:00:00Z",
    }


def carbon(verdict="APPROVED", **kwargs):
    return record(task="review-carbon", model=CARBON, family="google", verdict=verdict, second=True, **kwargs)


def muse(verdict="APPROVED", **kwargs):
    return record(task="review-muse", model=MUSE, family="meta", verdict=verdict, second=True, **kwargs)


def test_second_review_missing_holds_a_required_head_at_needs_seat():
    """Defect class: a High-tier or security PR lands on the reviewer of record alone."""
    assert sweep.lookup_verdict([record(task="review-sol")], SHA, "fleet", second_review=True).state == sweep.NEEDS_SEAT


def test_second_review_and_reviewer_of_record_together_approve():
    """Defect class: a correctly double-reviewed head is still refused."""
    rows = [record(task="review-sol"), carbon()]
    assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == "APPROVED"


def test_second_review_alone_never_approves():
    """Defect class: Carbon's approval stands in for the missing reviewer of record."""
    for required in (True, False):
        verdict = sweep.lookup_verdict([carbon()], SHA, "fleet", second_review=required)
        assert verdict.state == "needs-CF"


def test_untokened_carbon_approval_is_not_a_reviewer_of_record_or_second_review():
    """Defect class: a Carbon record without the recorder's second-review token counts as either seat."""
    untokened = record(task="review-carbon", model=CARBON, family="google")
    assert sweep.lookup_verdict([untokened], SHA, "fleet").state == "needs-CF"
    rows = [record(task="review-sol"), untokened]
    assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == sweep.NEEDS_SEAT
    # A listed model that also reviews of record (Muse) keeps that seat when untokened.
    ordinary = record(task="review-muse", model=MUSE, family="meta")
    assert sweep.lookup_verdict([ordinary], SHA, "fleet").state == "APPROVED"
    assert sweep.lookup_verdict([record(task="review-sol"), ordinary], SHA, "fleet",
                                second_review=True).state == sweep.NEEDS_SEAT


def test_second_review_token_on_another_model_poisons_instead_of_approving():
    """Defect class: a forged token on a non-Carbon record satisfies the Carbon seat."""
    forged = record(task="review-sol-2", model=CARBON, family="google", second=True)
    forged["body"] = forged["body"].replace(CARBON, "gemini-4-argon-medium")
    assert sweep.parse_marker(forged["body"]) is None
    rows = [record(task="review-sol"), forged]
    assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == "unknown"


def test_second_review_rejection_stands_over_both_approvals():
    """Defect class: a Carbon CHANGES_REQUESTED (or a timeout's BLOCKED) is outvoted by approvals."""
    for rejection in ("CHANGES_REQUESTED", "BLOCKED"):
        rows = [record(task="review-sol"), carbon(verdict=rejection)]
        assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == rejection


def test_second_review_is_required_on_the_coverage_path():
    """Defect class: an item-denominator head skips the second-review rule."""
    def seat(task, model, family):
        return record(
            task=task, model=model, family=family, review_profile="ukrainian", sources_mcp_call_count=3,
            denominator=("i1",), coverage=("i1",),
        )

    rows = [seat("review-uk-g", "gemini-3.8-flash-high", "google"), seat("review-uk-o", "gpt-6.1-sol", "openai")]
    assert sweep.lookup_verdict(rows, SHA, "fleet").state == "APPROVED"
    assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == sweep.NEEDS_SEAT
    # The second review counts only from a family other than every approving reviewer of record's.
    assert sweep.lookup_verdict([*rows, carbon()], SHA, "fleet", second_review=True).state == sweep.NEEDS_SEAT
    assert sweep.lookup_verdict([*rows, muse()], SHA, "fleet", second_review=True).state == "APPROVED"


def test_gemini_authored_required_head_takes_the_next_listed_family():
    """Defect class: a Gemini-authored PR waives the second review another listed family can give,
    or is held for one no listed family may give."""
    rows = [record(task="review-sol", authors=("gemini-4-carbon-medium",))]
    assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == sweep.NEEDS_SEAT
    assert sweep.lookup_verdict([*rows, muse(authors=("gemini-4-carbon-medium",))], SHA, "fleet",
                                second_review=True).state == "APPROVED"
    # Authors of every listed family but the reviewer of record's leave no second reviewer.
    every = (MUSE, "gemini-4-carbon-medium", "claude-opus-5-5")
    assert sweep.lookup_verdict([record(task="review-sol", authors=every)], SHA, "fleet",
                                second_review=True).state == "APPROVED"


def test_unattested_authors_keep_the_second_review_required():
    """Defect class: a record with no author attestation waives the second review."""
    rows = [record(task="review-sol", authors=())]
    assert sweep.lookup_verdict(rows, SHA, "fleet", second_review=True).state == sweep.NEEDS_SEAT


def test_head_that_needs_no_second_review_is_unchanged():
    """Defect class: the second-review rule leaks onto ordinary PRs."""
    assert sweep.lookup_verdict([record(task="review-sol")], SHA, "fleet").state == "APPROVED"
    rows = [record(task="review-sol"), carbon()]
    assert sweep.lookup_verdict(rows, SHA, "fleet").state == "APPROVED"


@pytest.mark.parametrize("repository,expected", [
    ("unit/public", sweep.NEEDS_SEAT),
    ("learn-ukrainian/machinery", "APPROVED"),
])
def test_readiness_report_holds_a_security_pr_without_the_carbon_second_review(monkeypatch, repository, expected):
    """Defect class: the read-only landing report calls a head ready that the enqueue gate refuses."""
    seen = []
    real = sweep.lookup_verdict

    def spy(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    class Adapter:
        def list_open_prs(self, _repository):
            return [{"number": 1, "headRefOid": SHA}]

        def identity(self):
            return "fleet"

        def comments(self, _repository, _number):
            return [record(task="review-sol")]

        def pr_file_rows(self, _repository, _number):
            return [{"filename": "lu_machinery/publish/github.py", "status": "modified"}]

        def queue_membership(self, _repository, _pr):
            return False

    monkeypatch.setattr(sweep, "lookup_verdict", spy)
    monkeypatch.setattr(sweep, "changes_learner_content", lambda _paths: False)
    monkeypatch.setattr(sweep, "classify_pr", lambda pr, verdict, **_: verdict)
    sweep.run(Adapter(), repository)
    assert [verdict.state for verdict in seen] == [expected]


@pytest.mark.parametrize("repository", ["learn-ukrainian/machinery", "learn-ukrainian/learn-ukrainian.github.io"])
@pytest.mark.parametrize("tokened", [True, False])
def test_carbon_rejection_blocks_even_with_p3_only_findings(repository, tokened):
    """Defect class: Carbon REQUEST_CHANGES is downgraded to approval by the P3 residual rule."""
    import json

    reply = json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "incorrect", "explanation": "Minor finding remains.", "confidence": 0.9},
        "findings": [{
            "id": "F1", "title": "Minor finding", "body": "A minor finding remains.",
            "priority": "P3", "confidence": 0.9, "category": "correctness",
            "location": {"path": "lu_machinery/publish/github.py", "start_line": 1, "end_line": 1, "claim_type": "present"},
            "verbatim": "unit", "why_wrong": "Minor defect.", "smallest_fix": "Correct it.", "sources": ["none"],
        }],
    })
    rejection = record(task="review-carbon", model=CARBON, family="google", verdict="CHANGES_REQUESTED",
                       reply=reply, second=tokened)
    assert sweep.recorded_p3_residuals(rejection["body"]) == ("F1",)
    assert sweep.lookup_verdict(
        [record(task="review-sol"), rejection], SHA, "fleet", second_review=True, repository=repository,
    ).state == "CHANGES_REQUESTED"

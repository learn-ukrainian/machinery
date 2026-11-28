"""Unit tests for canonical formal-review verdict and evidence parsing."""

from __future__ import annotations

import json

import pytest

from lu_machinery.fleet_comms.review_publication import (
    ReviewPublicationError,
    normalize_verdict,
    parse_verdict_token,
    resolve_verdict_and_evidence,
)
from lu_machinery.review.review_contract import ContractError, parse_reviewer_json

# ── verdict parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("APPROVED", "APPROVED"),
        ("approved", "APPROVED"),
        ("CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("changes_requested", "CHANGES_REQUESTED"),
        ("BLOCKED", "BLOCKED"),
        ("  blocked  ", "BLOCKED"),
    ],
)
def test_normalize_verdict_accepts_canonical_states(raw: str, expected: str) -> None:
    assert normalize_verdict(raw) == expected


def test_parse_verdict_token_from_text() -> None:
    body = "notes...\nVERDICT: CHANGES_REQUESTED\nmore"
    assert parse_verdict_token(body) == "CHANGES_REQUESTED"


def test_parse_verdict_token_missing() -> None:
    with pytest.raises(ReviewPublicationError, match="verdict_missing"):
        parse_verdict_token("no formal marker here")


@pytest.mark.parametrize("correctness", ["correct", "incorrect", "uncertain"])
def test_degenerate_review_evidence_is_always_blocked(correctness: str) -> None:
    """A bare reviewer label cannot grant or deny the formal merge gate."""
    verdict, evidence = resolve_verdict_and_evidence(
        {
            "schema_version": "code-review-findings.v1",
            "verdict": "BLOCKED",
            "overall": {
                "correctness": correctness,
                "explanation": "",
                "confidence": 0.9,
            },
            "findings": [],
        }
    )

    assert verdict == "BLOCKED"
    assert evidence is None


def test_degenerate_review_evidence_rejects_an_explicit_approved_verdict() -> None:
    with pytest.raises(ReviewPublicationError, match="review_evidence_verdict_mismatch"):
        resolve_verdict_and_evidence(
            {
                "schema_version": "code-review-findings.v1",
                "verdict": "APPROVED",
                "overall": {
                    "correctness": "correct",
                    "explanation": "",
                    "confidence": 0.9,
                },
                "findings": [],
            }
        )


# ── security findings (REVIEW-GO-1128 slice 1) ───────────────────────────────


def _security_payload(correctness: str, priority: str, *, category: str = "security") -> dict:
    return {
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": correctness, "explanation": "One finding.", "confidence": 0.9},
        "findings": [{
            "id": "F1", "title": "Link escape", "body": "A symlink escapes the root.", "priority": priority,
            "confidence": 0.9, "category": category,
            "location": {"path": "scripts/example.py", "start_line": 1, "end_line": 1, "claim_type": "present"},
            "verbatim": "value = shared_state", "why_wrong": "The link is classified by its text.",
            "smallest_fix": "Resolve the link first.", "sources": ["none"],
        }],
    }


def _envelope(stated: str, payload: dict) -> str:
    return "\n".join((f"VERDICT: {stated}", "SCORE: 8/10", "", "```json", json.dumps(payload, indent=2), "```", ""))


@pytest.mark.parametrize("priority", ["P2", "P3"])
def test_bare_correct_reply_with_security_finding_requests_changes(priority: str) -> None:
    """Defect class: security finding approved (a 'correct' reply with a security P2/P3 derived APPROVED)."""
    payload = _security_payload("correct", priority)
    assert parse_reviewer_json(json.dumps(payload)) == payload
    verdict, evidence = resolve_verdict_and_evidence(payload)
    assert verdict == "CHANGES_REQUESTED" and evidence is not None
    assert [finding.priority for finding in evidence.findings] == [priority]
    # The same priorities on a non-security finding stay follow-ups.
    assert resolve_verdict_and_evidence(_security_payload("correct", priority, category="bug"))[0] == "APPROVED"


@pytest.mark.parametrize("priority", ["P2", "P3"])
def test_explicit_approve_with_security_finding_is_refused(priority: str) -> None:
    """Defect class: security finding approved through an APPROVE envelope or verdict field (header must agree)."""
    payload = _security_payload("correct", priority)
    with pytest.raises(ContractError, match="review_envelope_verdict_mismatch"):
        parse_reviewer_json(_envelope("APPROVE", payload))
    with pytest.raises(ReviewPublicationError, match="review_evidence_verdict_mismatch"):
        resolve_verdict_and_evidence({**payload, "verdict": "APPROVED"})
    # A rejection envelope carrying the same finding stays readable.
    rejected = _security_payload("incorrect", priority)
    assert parse_reviewer_json(_envelope("REQUEST_CHANGES", rejected)) == rejected


@pytest.mark.parametrize("correctness", ["correct", "incorrect", "uncertain"])
@pytest.mark.parametrize("priority", ["P0", "P1", "P2", "P3"])
def test_security_finding_never_approves_at_any_correctness(correctness: str, priority: str) -> None:
    """Defect class: security finding approved; 'incorrect' and 'uncertain' never approve either."""
    verdict, _ = resolve_verdict_and_evidence(_security_payload(correctness, priority))
    assert verdict == "CHANGES_REQUESTED"
    clean = {**_security_payload(correctness, priority), "findings": []}
    assert resolve_verdict_and_evidence(clean)[0] == {
        "correct": "APPROVED", "incorrect": "CHANGES_REQUESTED", "uncertain": "BLOCKED",
    }[correctness]

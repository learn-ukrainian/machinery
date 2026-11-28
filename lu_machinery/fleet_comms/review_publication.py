"""Pure helpers for canonical formal-review verdict and evidence parsing.

Sealed-only publication planning (``SealedVerdict``, ``PublicationPlan``,
``parse_sealed_verdict_payload``, ``plan_publication``, and comment builders)
was retired under decision C-7371.  This module preserves the shared verdict
normalization and canonical ``code-review-findings.v1`` evidence parser used by
``review_contract``, ``verdict_parser``, ``record_cf_verdict``,
``review_ground_truth``, and ``model_stats_rows``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

VALID_VERDICTS = frozenset({"APPROVED", "CHANGES_REQUESTED", "BLOCKED"})
DEFAULT_GATE_KIND = "cross-family-review"
DEFAULT_STATUS_CONTEXT = "fleet/cross-family-review"
REVIEW_SCHEMA_VERSION = "code-review-findings.v1"

_VERDICT_LINE_RE = re.compile(
    r"\bVERDICT\s*:\s*(APPROVED|CHANGES_REQUESTED|BLOCKED)\b",
    re.IGNORECASE,
)

# A verdict a reviewer states inside a structured reply's explanation. This is
# the prose verdict-line grammar (``scripts/review/verdict_parser.py``
# ``_REVIEW_VERDICT_LINE_RE``, Markdown emphasis and code spans included),
# unanchored, because explanation prose states it mid-line.
_STATED_VERDICT_RE = re.compile(
    r"(?<![^\W_])VERDICT(?:[*_`\s]*:[*_`\s]*|[*_`\s]+)"
    r"(APPROVED?|REQUEST_CHANGES|CHANGES_REQUESTED|BLOCKED)(?![^\W_]|_+[^\W_])",
    re.IGNORECASE,
)
_STATED_VERDICT_TOKENS = {
    "APPROVE": "APPROVED",
    "APPROVED": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "CHANGES_REQUESTED": "CHANGES_REQUESTED",
    "BLOCKED": "BLOCKED",
}
# Finding priorities that block a merge; P2 and P3 are follow-ups.
BLOCKING_PRIORITIES = frozenset({"P0", "P1"})
# A finding of this category blocks at every priority: an unresolved security finding always
# blocks (review-loop policy, Findings).
SECURITY_CATEGORY = "security"


class ReviewPublicationError(ValueError):
    """Canonical review verdict or evidence parsing failed closed."""


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """Immutable projection of one validated canonical review finding."""

    finding_id: str
    title: str
    body: str
    priority: str
    confidence: float
    category: str
    path: str
    start_line: int
    end_line: int
    claim_type: str
    verbatim: str
    why_wrong: str
    smallest_fix: str
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
            "confidence": self.confidence,
            "category": self.category,
            "location": {
                "path": self.path,
                "start_line": self.start_line,
                "end_line": self.end_line,
                "claim_type": self.claim_type,
            },
            "verbatim": self.verbatim,
            "why_wrong": self.why_wrong,
            "smallest_fix": self.smallest_fix,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    """Validated review evidence retained with the formal verdict."""

    correctness: str
    explanation: str
    confidence: float
    findings: tuple[ReviewFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "overall": {
                "correctness": self.correctness,
                "explanation": self.explanation,
                "confidence": self.confidence,
            },
            "findings": [finding.to_dict() for finding in self.findings],
        }


def normalize_verdict(raw: Any) -> str:
    """Return a canonical APPROVED|CHANGES_REQUESTED|BLOCKED verdict."""
    if not isinstance(raw, str):
        raise ReviewPublicationError(
            "invalid_verdict: expected APPROVED|CHANGES_REQUESTED|BLOCKED"
        )
    verdict = raw.strip().upper()
    if verdict not in VALID_VERDICTS:
        raise ReviewPublicationError(
            f"invalid_verdict: {raw!r} "
            "(expected APPROVED|CHANGES_REQUESTED|BLOCKED)"
        )
    return verdict


def _canonical_evidence_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the canonical reviewer payload from a wrapper mapping."""
    return {
        field: payload[field]
        for field in ("schema_version", "overall", "findings")
        if field in payload
    }


def _has_canonical_review_evidence(payload: Mapping[str, Any]) -> bool:
    return "schema_version" in payload or "overall" in payload


def _is_degenerate_review_evidence(payload: Mapping[str, Any]) -> bool:
    """Recognize a reviewer that supplied neither explanation nor findings.

    The canonical schema deliberately rejects an empty explanation. Publication
    nevertheless needs to surface this exact reviewer failure rather than turn
    it into a clean-looking verdict or discard it with a generic schema error.
    """
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        return False
    findings = payload.get("findings")
    overall = payload.get("overall")
    if findings != [] or not isinstance(overall, Mapping):
        return False
    explanation = overall.get("explanation")
    return not isinstance(explanation, str) or not explanation.strip()


def parse_review_evidence(payload: Mapping[str, Any]) -> ReviewEvidence | None:
    """Validate and freeze canonical reviewer evidence for public rendering.

    ``None`` is reserved for the explicit no-evidence case: an empty explanation
    and no findings. All other malformed evidence fails closed before it can
    produce a misleading GitHub gate comment.
    """
    canonical = _canonical_evidence_fields(payload)
    if _is_degenerate_review_evidence(canonical):
        return None

    try:
        from lu_machinery.review.review_contract import ContractError, validate_reviewer_payload

        validated = validate_reviewer_payload(canonical)
    except ContractError as exc:
        raise ReviewPublicationError(f"review_evidence_invalid: {exc}") from exc

    overall = validated["overall"]
    findings = tuple(
        ReviewFinding(
            finding_id=finding["id"],
            title=finding["title"],
            body=finding["body"],
            priority=finding["priority"],
            confidence=float(finding["confidence"]),
            category=finding["category"],
            path=finding["location"]["path"],
            start_line=int(finding["location"]["start_line"]),
            end_line=int(finding["location"]["end_line"]),
            claim_type=finding["location"]["claim_type"],
            verbatim=finding["verbatim"],
            why_wrong=finding["why_wrong"],
            smallest_fix=finding["smallest_fix"],
            sources=tuple(finding["sources"]),
        )
        for finding in validated["findings"]
    )
    return ReviewEvidence(
        correctness=overall["correctness"],
        explanation=overall["explanation"],
        confidence=float(overall["confidence"]),
        findings=findings,
    )


def verdict_from_review_evidence(evidence: ReviewEvidence) -> str:
    """Derive the only gate verdict consistent with canonical review evidence.

    P0 and P1 findings block, and so does a security finding at any priority.
    Other P2 and P3 findings are follow-ups (review-loop policy, Severity), so
    code judged correct still approves with them; on code judged uncertain they
    request changes.
    """
    if evidence.correctness == "incorrect" or any(
        finding.priority in BLOCKING_PRIORITIES or finding.category == SECURITY_CATEGORY
        for finding in evidence.findings
    ):
        return "CHANGES_REQUESTED"
    if evidence.correctness == "correct":
        return "APPROVED"
    return "CHANGES_REQUESTED" if evidence.findings else "BLOCKED"


def _stated_verdicts(explanation: str) -> set[str]:
    """The canonical verdicts a reviewer's explanation states in prose."""
    return {
        _STATED_VERDICT_TOKENS[token.upper()]
        for token in _STATED_VERDICT_RE.findall(explanation)
    }


def _verdict_from_degenerate_evidence(_payload: Mapping[str, Any]) -> str:
    """Fail closed when a canonical review supplies no auditable evidence.

    A reviewer-provided correctness label without an explanation or finding is
    not evidence. It must never grant a green formal-review gate.
    """
    return "BLOCKED"


def resolve_verdict_and_evidence(
    payload: Mapping[str, Any], *, stated: str | None = None
) -> tuple[str, ReviewEvidence | None]:
    """Resolve a verdict and retain any canonical evidence from one payload.

    Legacy payloads may still carry only a top-level ``verdict``. Canonical
    ``code-review-findings.v1`` payloads derive the verdict from their verified
    meaning; an explicit conflicting token fails closed. ``stated`` is the
    reply's canonical verdict line outside the payload, if any.
    """
    raw_verdict = payload.get("verdict")
    explicit = normalize_verdict(raw_verdict) if raw_verdict is not None else None
    if not _has_canonical_review_evidence(payload):
        if explicit is None:
            raise ReviewPublicationError(
                "findings_json_missing_verdict: expected verdict or canonical review evidence"
            )
        return explicit, None

    evidence = parse_review_evidence(payload)
    derived = (
        verdict_from_review_evidence(evidence)
        if evidence is not None
        else _verdict_from_degenerate_evidence(payload)
    )
    if derived == "APPROVED" and evidence is not None:
        # An approval never outranks a rejection the reviewer states anywhere:
        # every explanation token counts, quoted ones included. The verdict field
        # and the line outside the payload hold an approval only when it carries
        # follow-ups; without findings their conflict refuses as a mismatch.
        claimed = _stated_verdicts(evidence.explanation)
        if evidence.findings:
            claimed |= {verdict for verdict in (explicit, stated) if verdict is not None}
        if claimed - {"APPROVED"}:
            derived = "CHANGES_REQUESTED"
    if explicit is not None and explicit != derived:
        raise ReviewPublicationError(
            f"review_evidence_verdict_mismatch: verdict={explicit} evidence={derived}"
        )
    return derived, evidence


def parse_verdict_token(text: str) -> str:
    """Extract a VERDICT line from free text without retaining the body."""
    if not isinstance(text, str) or not text.strip():
        raise ReviewPublicationError("verdict_missing: empty payload")
    match = _VERDICT_LINE_RE.search(text)
    if not match:
        raise ReviewPublicationError(
            "verdict_missing: expected VERDICT: APPROVED|CHANGES_REQUESTED|BLOCKED"
        )
    return normalize_verdict(match.group(1))


__all__ = [
    "BLOCKING_PRIORITIES",
    "DEFAULT_GATE_KIND",
    "DEFAULT_STATUS_CONTEXT",
    "REVIEW_SCHEMA_VERSION",
    "SECURITY_CATEGORY",
    "VALID_VERDICTS",
    "ReviewEvidence",
    "ReviewFinding",
    "ReviewPublicationError",
    "normalize_verdict",
    "parse_review_evidence",
    "parse_verdict_token",
    "resolve_verdict_and_evidence",
    "verdict_from_review_evidence",
]

#!/usr/bin/env python3
"""Record a completed branch-pinned cross-family or explicit red-team review."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import cache, partial
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from lu_machinery.review.review_lineage import ProviderCrashRecovery, ReviewRun

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# The machinery package's directory name, which its files' checkout paths start with, from this module's
# unresolved location: a symlinked package keeps its checkout name, and a short import spelling
# (the package directory on ``sys.path``) cannot shorten it.
_MACHINERY = Path(os.path.abspath(__file__)).parents[1].name
# This recorder's checkout-relative path.
RECORDER_PATH = f"{_MACHINERY}/review/record_cf_verdict.py"

if __name__ == "__main__":
    # Re-exec before importing recorder dependencies, as scripts/dispatch.py does.
    try:
        from lu_machinery.dispatcher.primary import exec_primary

        exec_primary(sys.argv[1:], entry=Path(RECORDER_PATH), refuse=sys.exit)
    except (ImportError, OSError, RuntimeError, SystemExit) as exc:
        print(json.dumps({"admitted": False, "refusal": f"RECORDER_PRIMARY_REFUSED: {exc}"}, sort_keys=True))
        raise SystemExit(2) from None

import yaml

from lu_machinery.agent_identity import normalize_seat
from lu_machinery.ci.deploy_paths import DeployPathsError
from lu_machinery.common import json_depth
from lu_machinery.common.jsonl import jsonl_lines
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.fleet.claude_account_lane import ClaudeAccount, account_from_record
from lu_machinery.fleet_comms.review_publication import (
    DEFAULT_STATUS_CONTEXT,
    REVIEW_SCHEMA_VERSION,
    SECURITY_CATEGORY,
    ReviewPublicationError,
)
from lu_machinery.fleet_comms.review_publisher import post_commit_status
from lu_machinery.identity import _VALID_CONCRETE_FAMILIES
from lu_machinery.model_families import canonical_cursor_model, is_cursor_auto_selector
from lu_machinery.opsec.prepublish import (
    PublishBlocked,
    absolute_path_spans,
    check_texts,
    normalize_for_scan,
    normalize_repository,
    publication_boundary,
    publication_cli,
)
from lu_machinery.orchestration import integration_sweep
from lu_machinery.orchestration.fleet_repos import (
    FleetRepo,
    FleetRepoError,
    load_fleet_repos,
    repository_identity,
    repository_slugs,
    resolve_fleet_repo_by_slug,
    resolve_payload_fleet_repo,
    same_repository,
)
from lu_machinery.orchestration.integration_sweep import (
    CHUNK_VOIDS,
    ITEM_ID_RE,
    MARKER_PREFIX,
    NEEDS_SEAT,
    REJECTED,
    REVIEW_TASK_ID,
    SECOND_REVIEW_LINE,
    SECOND_REVIEW_TOKEN,
    SHA,
    TRUSTED_ASSOCIATIONS,
    GitHubAdapter,
    SweepError,
    _author_login,
    is_second_review,
    is_second_review_only,
    is_second_reviewer_model,
    lookup_verdict,
    parse_marker,
    pr_file_paths,
    reviews_of_record,
)
from lu_machinery.orchestration.task_record_store import (
    ARCHIVE_DIR_NAME,
    TaskIdentityMismatch,
    is_identical_copy,
    misnamed_record_refusal,
    read_listed_task_record,
    task_archive_lock,
)
from lu_machinery.publish.github import CfVerdictRecord, Request, request_run
from lu_machinery.review.app_bot_authorship import VERIFIED_APP_BOT, listing_app_bot_login, verified_app_bot_login
from lu_machinery.review.language_lane import is_ukrainian_review
from lu_machinery.review.model_catalog import (
    REVIEW_ACTIVITY,
    activity_role_refusal,
    load_model_catalog,
    resolve_catalog_model_id,
)
from lu_machinery.review.review_contract import (
    ContractError,
    ItemChunk,
    captured_sources_events,
    declares_incomplete_explanation,
    item_judgments_verdict,
    judge_item_reply,
    load_frozen_item_packet,
    parse_reviewer_json,
    reply_has_item_judgments,
    validate_ukrainian_reply_claims,
)
from lu_machinery.review.reviewer_resolver import (
    FORMAL_CURSOR_REVIEW_MODELS,
    REVIEW_CANDIDATES,
    UNKNOWN_AUTHOR_FAMILY,
    UNRESOLVED_AUTHOR_FAMILIES,
    ResolverInputs,
    ReviewerResolution,
    evaluate_candidate,
    is_mixed_family_set,
    mixed_family_review,
    mixed_family_scope,
    normalize_review_task_id,
    red_team_seat_refusal,
    resolve_author_family,
    resolve_family,
    resolve_reviewer,
    review_task_id_comparison_keys,
)
from lu_machinery.review.role_resolution import resolve_routing_reference
from lu_machinery.review.security_paths import git_changed_paths, is_security_sensitive_change
from lu_machinery.review.subject_seat import prepare_subject_exclusion
from lu_machinery.review.target_resolution import TargetResolutionError, resolve_review_repository
from lu_machinery.review.tiers import ReviewTier, classify_pr_files
from lu_machinery.review.ukrainian_seats import (
    UKRAINIAN_SEAT_ROUTES,
    attested_author_families,
    required_ukrainian_seats,
)
from lu_machinery.review.verdict_parser import (
    _REVIEW_VERDICT_LINE_RE,
    MISSING_CAPABILITIES_FIELD,
    _commonmark_lines,
    _findings_json_verdicts,
    _top_level_json_values,
    _without_code_fences,
    _without_inline_json,
    checked_findings_payload,
    declares_incomplete_review,
    declares_missing_capabilities,
    invalid_findings_block,
    recognized_verdicts,
    review_score,
)

NORMALIZED = {
    "APPROVE": "APPROVED",
    "APPROVED": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "CHANGES_REQUESTED": "CHANGES_REQUESTED",
    "BLOCKED": "BLOCKED",
}
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z")
MAX_COMMENT_BYTES = 65_000
# Kimi CLI task records can identify the harness without naming its model;
# this harness is single-family and always runs Moonshot models.
SINGLE_FAMILY_HARNESSES = {"kimi": "moonshot"}
# ``resolved_model_source`` values the Cursor adapter writes only when the model
# came out of the runtime's own output (``scripts/agent_runtime/adapters/cursor.py``
# ``parse``: stream-json stdout, this invocation's transcript, stderr JSON events).
# The ``unattested-harness`` / ``pending`` / ``unknown`` fallbacks and any other
# value are not runtime reports, so a receipt carrying them proves no model.
RUNTIME_REPORTED_MODEL_SOURCES = frozenset({"cursor-stream-json", "cursor-transcript", "cursor-stderr-json"})
# Kimi never reviews. Native Grok requires its own runtime attestation (#9769).
NATIVE_NON_REVIEWER_FAMILIES = frozenset({"xai", "moonshot"})
# Review rounds per PR lineage, counted on the reviewed heads of the current PR.
# A target repository's own policy may declare up to five.
DEFAULT_REVIEW_ROUNDS = 3
MAX_DECLARED_REVIEW_ROUNDS = 5
# A target repository whose own review policy sets another automatic budget declares it here (#12584).
REVIEW_BUDGET_POLICY = machinery_path("config/review_budget_policy.yaml")


class RecordError(RuntimeError):
    """A review cannot be bound to a trustworthy exact-head verdict."""


NESTED_TOO_DEEPLY = "review reply is nested too deeply to read its verdict"
# Deepest JSON this module reads (json_depth): reviewer replies.
_JSON_MAX_DEPTH = 512


def normalize_verdict(reply: str) -> str:
    try:
        return _normalize_verdict(reply)
    except RecursionError:
        # The JSON decoder's depth limit: the verdict cannot be read, so it is refused by name.
        raise RecordError(NESTED_TOO_DEEPLY) from None


def _normalize_verdict(reply: str) -> str:
    tokens = {NORMALIZED[token.upper()] for token in recognized_verdicts(reply)}
    visible = _without_code_fences(reply, keep_unclosed=False)
    verdict_line = any(_REVIEW_VERDICT_LINE_RE.match(line) for line in _commonmark_lines(visible))
    # The verdict line is the reply's stated verdict; it holds a findings block that approves with follow-ups.
    stated = next(iter(tokens)) if verdict_line and len(tokens) == 1 else None
    # Every top-level findings block must pass the verifier's schema, whether or not a verdict line is present.
    if reason := invalid_findings_block(reply, stated=stated):
        beside = " beside a verdict line" if verdict_line else ""
        raise RecordError(f"reply carries an invalid findings block{beside}: {reason}")
    if declares_incomplete_review(reply):
        raise RecordError("inconclusive: reviewer declares an incomplete review")
    if verdict_line:
        findings = {NORMALIZED[token] for token in _findings_json_verdicts(reply, stated=stated)}
        offset = 0
        for line in _commonmark_lines(visible, keepends=True):
            line_start = offset
            offset += len(line)
            match = re.match(r" {0,3}\{", line)
            if match is None:
                continue
            start = line_start + match.end() - 1
            try:
                _, end = json_depth.raw_decode(visible, start, max_depth=_JSON_MAX_DEPTH)
            except ValueError:
                continue
            findings.update(NORMALIZED[token] for token in _findings_json_verdicts(visible[start:end], stated=stated))
        if findings and findings != tokens:
            raise RecordError("reply carries a findings payload contradicting a verdict line")
    if len(tokens) != 1:
        raise RecordError("review reply has missing or ambiguous VERDICT token")
    verdict = tokens.pop()
    # A reviewer that reports missing capabilities cannot approve, wherever the reply declares them.
    if verdict != "BLOCKED" and declares_missing_capabilities(reply):
        raise RecordError(
            f"reply carries {MISSING_CAPABILITIES_FIELD} with verdict {verdict}; only a BLOCKED reply may"
        )
    return verdict


def _review_verdict_and_count(reply: str) -> tuple[str, int | Literal["unknown"]]:
    """Use canonical validated findings; prose supplies no reliable count."""
    try:
        payload = json_depth.loads(reply, max_depth=_JSON_MAX_DEPTH)
    except ValueError:
        payload = None
    except RecursionError:
        raise RecordError(NESTED_TOO_DEEPLY) from None
    if isinstance(payload, dict) and payload.get("schema_version") == REVIEW_SCHEMA_VERSION:
        # The closeout verifier's schema judges the reply itself, not a canonical subset of it.
        try:
            verdict, evidence = checked_findings_payload(payload)
        except ContractError as exc:
            raise RecordError(f"structured review verdict or findings invalid: {exc}") from None
        except ReviewPublicationError:
            raise RecordError("structured review verdict or findings invalid") from None
        # The decoded object is read, so leading whitespace that Markdown makes a code block hides nothing.
        if declares_incomplete_review(reply, findings_payloads=(payload,)) or declares_incomplete_explanation(payload):
            raise RecordError("inconclusive: reviewer declares an incomplete review")
        return verdict, len(evidence.findings) if evidence is not None else "unknown"
    verdict = normalize_verdict(reply)
    # A whole json fence or the review-loop envelope: its explanation is read line by line too.
    try:
        whole = parse_reviewer_json(reply)
    except ContractError:
        whole = None
    if whole is not None and declares_incomplete_explanation(whole):
        raise RecordError("inconclusive: reviewer declares an incomplete review")
    return verdict, "unknown"


_SECURITY_VERDICT = re.compile(r"(?i)\*{0,2}security(?:[_\- ]+)verdict\*{0,2}\s*[:\u2014\u2013\-=]?\s*")
_SECURITY_PASS = re.compile(r"(?i)\*{0,2}PASS\*{0,2}\.?\s*(?:\n|$)")
_RESIDUAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
TRUNCATION_FLAG = "[Review reply truncated to fit GitHub's comment limit.]"
REPAIR_WITHHELD = "Repair round: P3 mapping withheld"


class _DuplicateKey(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _DuplicateKey
    return dict(pairs)


def _not_json_constant(token: str) -> Any:
    # NaN and Infinity are a Python decoder extension, not JSON.
    raise ValueError(token)


def _strings(value: Any) -> Iterator[str]:
    """Every decoded string value in a JSON value, so escapes cannot disguise a declaration."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _security_verdict_blocks(reply: str) -> bool:
    """True when a security verdict is present and the whole value is not PASS.

    The key is ``SECURITY`` plus ``_``, ``-`` or a space, then ``VERDICT``.
    A pass is the token ``PASS``, optional closing emphasis or a period, and
    then only whitespace to the end of the line. ``PASS_WITH_CONDITIONS``,
    ``PASS?`` and ``PASS (pending F1)`` are not a pass.
    """
    return any(_SECURITY_PASS.match(reply, match.end()) is None for match in _SECURITY_VERDICT.finditer(reply))


# The security sign-off: the first line of the sealed reply's ``overall.explanation`` is exactly this text.
# Schema-bound review dispatch seals only the findings object (``dispatcher/worker.py``), so the sign-off
# lives inside it.
SECURITY_ATTESTATION = "SECURITY_VERDICT: PASS"


@dataclass(frozen=True)
class _SecuritySignals:
    """What a reply states about security, read the way every verdict reader reads it.

    ``payload`` is the reply as ``parse_reviewer_json`` reads it, else None. ``texts`` is the reply's
    visible prose (code fences and top-level JSON values removed) and every decoded string of each
    distinct JSON reading (``payload`` and the Markdown top-level values), so neither a JSON escape nor
    an indented code block hides a declaration or a finding. ``None`` for ``texts`` means the reply's
    JSON cannot be decided.
    """

    payload: Mapping[str, Any] | None
    texts: tuple[str, ...] | None
    security_findings: tuple[str, ...]

    @property
    def open(self) -> bool:
        """An undecidable reply, a security verdict that is not PASS, or a canonical security finding."""
        return self.texts is None or bool(self.security_findings) or any(
            _security_verdict_blocks(text) for text in self.texts
        )

    @property
    def declarations(self) -> int:
        return sum(len(_SECURITY_VERDICT.findall(text)) for text in self.texts or ())


_UNDECIDABLE = _SecuritySignals(None, None, ())


def _security_signals(reply: str) -> _SecuritySignals:
    values: list[Any] = []
    try:
        payload: dict[str, Any] | None = parse_reviewer_json(reply)
    except ContractError:
        payload = None
        # The verdict reader decodes a whole-JSON reply leniently (``_review_verdict_and_count``). One the
        # strict reader refuses (a duplicate key, a non-JSON constant) cannot be decided.
        try:
            json_depth.loads(reply, max_depth=_JSON_MAX_DEPTH)
        except ValueError:
            pass
        except RecursionError:
            return _UNDECIDABLE
        else:
            return _UNDECIDABLE
    else:
        values.append(payload)
    try:
        values.extend(value for value in _top_level_json_values(reply) if value not in values)
        prose = _without_code_fences(_without_inline_json(reply), keep_unclosed=False)
    except (ValueError, RecursionError):
        return _UNDECIDABLE
    findings = tuple(dict.fromkeys(
        str(finding.get("id"))
        for value in values
        if isinstance(value, Mapping) and isinstance(value.get("findings"), list)
        for finding in value["findings"]
        if isinstance(finding, Mapping) and finding.get("category") == SECURITY_CATEGORY
    ))
    return _SecuritySignals(payload, (prose, *(text for value in values for text in _strings(value))), findings)


def _signed_off(reply: str, signals: _SecuritySignals) -> bool:
    """Whether the reply is one JSON object whose ``overall.explanation`` opens with the one ``SECURITY_ATTESTATION``.

    The object is the reply as ``parse_reviewer_json`` reads it, bare or as one whole json fence; the review-loop
    envelope carries text beside the object, so it never signs off. The declaration is counted across the visible
    prose and every decoded string, and must be that line alone.
    """
    payload = signals.payload
    if payload is None or reply.lstrip(" \t\r\n").startswith("VERDICT:") or signals.declarations != 1:
        return False
    overall = payload.get("overall")
    explanation = overall.get("explanation") if isinstance(overall, Mapping) else None
    return isinstance(explanation, str) and explanation.split("\n", 1)[0] == SECURITY_ATTESTATION


def _critical_review_seat(task: Mapping[str, Any], model: str) -> bool:
    """Whether the reviewer's catalog seat holds the ``critical_review`` role (critical and security sign-off)."""
    return any("critical_review" in candidate.model_roles for candidate in _review_candidates(task, model))


def _inherited_security_reviews(
    comments: Sequence[Mapping[str, Any]], login: str, *, task_root: Path, task_id: str, started: str,
    repository: str, number: int, branch: str,
) -> tuple[str, ...]:
    """The other reviews of this PR whose sealed reply has an open security signal, or cannot be read.

    A later reply that omits such a finding has not resolved it: only a security sign-off does. Two sources are
    read, so a review counts whether or not its verdict was published:

    - every finished review run of this PR (``review_lineage.lineage_runs``, hot and archived records), with
      its sealed reply;
    - every trusted verdict marker. Its reply is read from the task record of the run the marker names;
      without one, from the unedited comment when it publishes the whole reply. A review with neither
      counts, because nothing shows it was clean.

    A task ledger that cannot be read refuses (``LineageRefused``).
    """
    from lu_machinery.review.review_lineage import lineage_runs

    found: list[str] = []
    for run in lineage_runs(task_root, repository=repository, number=number, branch=branch):
        if run.record.get("status") != "done" or (run.task_id, _normalized_started(run.record)) == (task_id, started):
            continue
        if run.reply is None:
            found.append(f"{run.task_id} (reply unavailable)")
        elif _security_signals(run.reply).open:
            found.append(run.task_id)
    for index, marker in _trusted_markers(comments, login):
        if (marker["task"], marker["started"]) == (task_id, started):
            continue
        prior: str | None = None
        try:
            record, sealed = _task(marker["task"], task_root)
        except RecordError:
            pass
        else:
            if record.get("worktree_base_sha") == marker["sha"] and _normalized_started(record) == marker["started"]:
                prior = sealed
        if prior is None:
            prior = _published_reply(comments[index])
        if prior is None:
            found.append(f"{marker['task']} (reply unavailable)")
        elif _security_signals(prior).open:
            found.append(marker["task"])
    return tuple(dict.fromkeys(found))


_PUBLISHED_REPLY = re.compile(
    r"\n<details><summary>Reviewer's reply</summary>\n\n(?P<reply>.*)\n\n</details>\n\n", re.DOTALL,
)


def _published_reply(comment: Mapping[str, Any]) -> str | None:
    """The whole reply an unedited verdict comment publishes (``build_comment``), else None (withheld or truncated)."""
    body = comment.get("body")
    if not isinstance(body, str) or comment.get("created_at") != comment.get("updated_at"):
        return None
    match = _PUBLISHED_REPLY.search(body)
    if match is None or TRUNCATION_FLAG in match["reply"]:
        return None
    return match["reply"]


def _require_security_approval(
    reply: str, *, critical: bool, critical_seat: bool, inherited: Sequence[str],
) -> None:
    """Refuse an approval an open security signal contradicts, or one that lacks a required security sign-off.

    The sign-off is required on a critical review (security path, critical risk or the security profile) and after
    any other review of the PR raised a security finding: the reply's ``overall.explanation`` opens with the one
    ``SECURITY_ATTESTATION`` line (``_signed_off``), from a ``critical_review`` seat.
    """
    signals = _security_signals(reply)
    if signals.texts is None:
        raise RecordError("SECURITY_APPROVAL_REFUSED: the reply's JSON cannot be decided")
    if signals.security_findings:
        raise RecordError(
            "SECURITY_APPROVAL_REFUSED: the reply carries a security finding: " + ", ".join(signals.security_findings)
        )
    if signals.open:
        raise RecordError("SECURITY_APPROVAL_REFUSED: the reply carries a security verdict that is not PASS")
    if not critical and not inherited:
        return
    why = "critical review" if critical else "another review of this PR raised a security finding (" + (
        ", ".join(inherited) + ")"
    )
    if not critical_seat:
        raise RecordError(f"SECURITY_APPROVAL_REFUSED: {why}: the sign-off needs a critical_review seat")
    if not _signed_off(reply, signals):
        raise RecordError(
            f"SECURITY_APPROVAL_REFUSED: {why}: APPROVE needs a reply that is one JSON object whose "
            f"overall.explanation opens with the line {SECURITY_ATTESTATION!r}, the only security verdict in the reply"
        )


def _label_names(labels: Any) -> set[str]:
    """Label names on a PR or issue payload the recorder already read."""
    if not isinstance(labels, list):
        return set()
    names: set[str] = set()
    for item in labels:
        if isinstance(item, str):
            names.add(item.strip().casefold())
        elif isinstance(item, Mapping):
            name = item.get("name")
            if isinstance(name, str):
                names.add(name.strip().casefold())
    return names


def _security_labeled(pr: Mapping[str, Any]) -> bool:
    """True when the PR, or an issue it closes, carries the security label."""
    if "security" in _label_names(pr.get("labels")):
        return True
    refs = pr.get("closingIssuesReferences")
    if not isinstance(refs, list):
        return False
    return any(isinstance(ref, Mapping) and "security" in _label_names(ref.get("labels")) for ref in refs)


def _security_sensitive(*, task: Mapping[str, Any], reply: str, pr: Mapping[str, Any]) -> bool:
    """A review whose public comment must carry the finding count and not the findings.

    The signals are the ones this recorder already holds: the task's review profile,
    the reply (a security verdict, a canonical security finding, or JSON that cannot
    be decided), and labels on the PR view (linked issues included).
    """
    profile = str(task.get("review_profile") or "").strip().casefold()
    signals = _security_signals(reply)
    return (
        profile == "security"
        or signals.texts is None
        or bool(signals.security_findings)
        or _SECURITY_VERDICT.search(reply) is not None
        or signals.declarations > 0
        or _security_labeled(pr)
    )


def _p3_finding_ids(reply: str) -> tuple[str, ...]:
    """List the IDs of a structured review that has only P3 findings."""
    try:
        _, evidence = checked_findings_payload(parse_reviewer_json(reply))
    except (ContractError, ReviewPublicationError):
        return ()
    if evidence is None or any(finding.priority != "P3" for finding in evidence.findings):
        return ()
    ids = tuple(finding.finding_id for finding in evidence.findings)
    if len(set(ids)) != len(ids) or any(_RESIDUAL_ID.fullmatch(finding_id) is None for finding_id in ids):
        return ()
    return ids


def _require_sources_mcp_calls(task: dict[str, Any]) -> None:
    """Refuse Ukrainian verdicts without positive runtime Sources evidence (#10074)."""
    if not is_ukrainian_review(task):
        return
    count = task.get("sources_mcp_call_count")
    if type(count) is not int or count < 0:
        raise RecordError("Ukrainian review Sources MCP call count unknown; runtime evidence required")
    if count == 0:
        raise RecordError("Ukrainian review has zero Sources MCP calls")


@publication_boundary(RecordError)
def _run_json(args: list[str], *, input_text: str | None = None) -> Any:
    try:
        process = request_run(args, input=input_text, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecordError("GitHub lookup or publication unavailable") from exc
    if process.returncode:
        raise RecordError((process.stderr or process.stdout or "GitHub command failed")[:500])
    try:
        return json_depth.loads(process.stdout, max_depth=_JSON_MAX_DEPTH)
    except ValueError as exc:
        raise RecordError("GitHub returned invalid JSON") from exc


def _pages(args: list[str]) -> list[dict[str, Any]]:
    data = _run_json(Request(args.verb, **args.fields, paginate=True, slurp=True))
    if not isinstance(data, list) or not all(isinstance(page, list) for page in data):
        raise RecordError("paginated GitHub data incomplete")
    items = [item for page in data for item in page]
    if not all(isinstance(item, dict) for item in items):
        raise RecordError("paginated GitHub data malformed")
    return items


def live_app_bot_login(repository: str, sha: str) -> str | None:
    """The allowlisted app bot GitHub reports as ``sha``'s author, read live for that SHA (#10531).

    Every lookup failure or disagreement returns None, which keeps the commit's
    authorship unknown and refused.
    """
    try:
        commit = _run_json(Request("read-commit", repo=_review_github(repository).active, sha=sha))
    except RecordError:
        return None
    return verified_app_bot_login(commit, sha, repository)


def _hot_or_archived(task_root: Path, name: str) -> Path:
    """``task_root / name``, else its copy in ``archive/`` (#8625), else the hot path."""
    hot = task_root / name
    archived = task_root / ARCHIVE_DIR_NAME / name
    return archived if not hot.exists() and archived.exists() else hot


def _merge_proof_object_store(start: Path | None = None) -> Path:
    """Find the checkout's shared objects without running Git or reading config."""
    cwd = start or Path.cwd()
    for root in (cwd, *cwd.parents):
        marker = root / ".git"
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: ") or "\n" in text:
                raise OSError("invalid Git directory pointer")
            git_dir = (root / text[8:]).resolve(strict=True)
        else:
            continue
        common_file = git_dir / "commondir"
        if common_file.is_file():
            git_dir = git_dir / common_file.read_text(encoding="utf-8").strip()
        objects = (git_dir / "objects").resolve(strict=True)
        if not objects.is_dir():
            raise OSError("Git object store unavailable")
        return objects
    raise OSError("Git checkout unavailable")


@contextmanager
def isolated_object_store(checkout: Path | None = None) -> Iterator[dict[str, str]]:
    """A Git environment that reads the checkout's objects and writes only to a throwaway store.

    The bare repository imports no init templates, config, refs, grafts,
    attributes or index from the writable shared Git dir, and objects a proof
    writes (``merge-tree --write-tree``) never reach the shared store. Raises
    ``OSError`` when the checkout's object store is unavailable.
    """
    objects = _merge_proof_object_store(checkout)
    with TemporaryDirectory(prefix="cf-merge-") as isolated:
        proof_dir = Path(isolated)
        (proof_dir / "objects/info").mkdir(parents=True)
        (proof_dir / "refs").mkdir()
        (proof_dir / "HEAD").write_text("ref: refs/heads/proof\n", encoding="utf-8")
        (proof_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            # Commit graphs can also be read from the alternate object store.
            f"\tcommitGraph = false\n\tattributesFile = {os.devnull}\n",
            encoding="utf-8",
        )
        (proof_dir / "objects/info/alternates").write_text(
            json.dumps(str(objects), ensure_ascii=False) + "\n", encoding="utf-8"
        )
        yield {
            **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            "GIT_DIR": isolated,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(proof_dir / "no-global-config"),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
        }


CLEAN_BASE_MERGE = "clean-base-merge"
CLEAN_SYNC_MERGE = "clean-sync-merge"


def _is_clean_base_merge(entry: dict[str, Any], base_sha: str, *, checkout: Path | None = None) -> bool:
    """Whether ``entry`` is a Git-proven conflict-free merge of the base into the branch."""
    return _clean_merge_source(entry, base_sha, checkout=checkout) == CLEAN_BASE_MERGE


def _clean_merge_source(
    entry: dict[str, Any],
    base_sha: str,
    *,
    checkout: Path | None = None,
    range_shas: frozenset[str] = frozenset(),
) -> str | None:
    """Bind a conflict-free merge to commit metadata and raw local objects.

    Returns ``CLEAN_BASE_MERGE`` when the second parent is a base ancestor, or
    ``CLEAN_SYNC_MERGE`` when every parent is a base ancestor or one of
    ``range_shas``, the PR's own commits, each attributed on its own. In both
    cases the tree must equal Git's merge of the two parents, so the merge adds
    no content. Anything else returns None.

    ``checkout`` locates the object store; the default is the current directory.
    """
    commit_sha = entry.get("sha")
    commit_data = entry.get("commit")
    tree_data = commit_data.get("tree") if isinstance(commit_data, dict) else None
    tree = tree_data.get("sha") if isinstance(tree_data, dict) else None
    parent_data = entry.get("parents")
    if not isinstance(parent_data, list) or len(parent_data) != 2:
        return None
    parents = [parent.get("sha") if isinstance(parent, dict) else None for parent in parent_data]
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in [commit_sha, base_sha, tree, *parents]):
        return None
    git = ["git"]
    try:
        with isolated_object_store(checkout) as env:
            commit = subprocess.run(
                [*git, "cat-file", "commit", commit_sha],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            headers = commit.stdout.partition("\n\n")[0].splitlines()
            if [line[5:] for line in headers if line.startswith("tree ")] != [tree]:
                return None
            if [line[7:] for line in headers if line.startswith("parent ")] != parents:
                return None
            base = subprocess.run(
                [*git, "cat-file", "-e", f"{base_sha}^{{commit}}"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if base.returncode:
                raise RecordError("base object not available locally; fetch and retry")

            def in_base(sha: str) -> bool:
                probe = subprocess.run(
                    [*git, "merge-base", "--is-ancestor", sha, base_sha],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if probe.returncode not in (0, 1):
                    raise subprocess.SubprocessError("ancestry unavailable")
                return probe.returncode == 0

            source = CLEAN_BASE_MERGE
            if not in_base(parents[1]):
                # A sync merge of the PR's own commits: each is attributed on its own.
                if not all(sha in range_shas or in_base(sha) for sha in parents):
                    return None
                source = CLEAN_SYNC_MERGE
            merged = subprocess.run(
                [*git, "merge-tree", "--write-tree", "--no-messages", *parents],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Missing objects, conflicts, unsupported Git, and timeouts prove no exemption.
        return None
    return source if merged.stdout.strip() == tree else None


@dataclass(frozen=True)
class CommitAttribution:
    """One author contribution to a branch commit, with its proven family.

    ``family`` is None only for a Git-proven clean base or sync merge, which authors nothing.
    """

    sha: str | None
    family: str | None
    source: str
    task_id: str | None = None
    record_task_id: str | None = None
    author_models: tuple[str, ...] = ()
    authorship: str | None = None


def _author_model_family(harness: str, model: str) -> str:
    """Resolve a model in its harness context; only Cursor accepts Auto selectors."""
    cursor = normalize_seat(harness.strip().lower().removesuffix("-tools")) == "cursor"
    if is_cursor_auto_selector(model) and not cursor:
        return UNKNOWN_AUTHOR_FAMILY
    return resolve_author_family(f"cursor:{model}" if cursor else model)


def _is_catalog_model_trailer(model: str) -> bool:
    """Accept explicit model forms, never arbitrary task-id suffixes."""
    catalog = load_model_catalog()
    model_id = resolve_catalog_model_id(model, catalog)
    if model_id is None:
        return False
    aliases = (model_id, *catalog["models"][model_id].get("aliases", []))
    pattern = (
        "(?:" + "|".join(re.escape(alias) for alias in aliases) + ")"
        r"(?:-(?:low|medium|high|xhigh|max))?(?:\[[^\[\]\s]+\])?"
    )
    candidates = [model, *(model[index + 1 :] for index, char in enumerate(model) if char in "/:")]
    return any(re.fullmatch(pattern, candidate, flags=re.IGNORECASE) for candidate in candidates)


def _attribute_commit(
    entry: dict[str, Any],
    *,
    repository: str,
    task_root: Path,
    base_sha: Callable[[], str],
    checkout: Path | None = None,
    unknown_history: Iterable[str] = (),
    app_bot_login: Callable[[str], str | None] | None = None,
    range_shas: frozenset[str] = frozenset(),
) -> CommitAttribution:
    """Resolve one commit's author family, retaining unresolvable authors as Unknown.

    ``entry`` has the GitHub commit-listing shape (``sha``, ``commit.message``,
    ``commit.tree.sha``, ``parents``). ``base_sha`` is called only when an
    untrailered commit needs the clean-merge proof; ``range_shas`` are the PR's
    own commits, which a clean sync merge may join. ``app_bot_login`` is called
    only for an untrailered commit that is neither a clean merge nor
    admitted unknown history; a login is GitHub's live proof that an
    allowlisted app bot authored it (#10531), recorded as ``verified_app_bot``
    without any model.
    """
    commit_sha = entry.get("sha")
    message = (entry.get("commit") or {}).get("message")
    if not isinstance(message, str):
        raise RecordError("commit message unavailable")
    trailers = re.findall(r"(?m)^X-Agent:\s*([^\s]+)\s*$", message)
    if (
        not trailers
        and not re.search(r"(?m)^X-Agent:", message)
        and isinstance(commit_sha, str)
        and SHA.fullmatch(commit_sha)
    ):
        merge_source = _clean_merge_source(entry, base_sha(), checkout=checkout, range_shas=range_shas)
        if merge_source is not None:
            return CommitAttribution(commit_sha, None, merge_source)
    if not trailers and not re.search(r"(?m)^X-Agent:", message) and commit_sha in unknown_history:
        return CommitAttribution(
            commit_sha, UNKNOWN_AUTHOR_FAMILY, "unknown_historical", authorship="unknown_historical"
        )
    if (
        not trailers
        and not re.search(r"(?m)^X-Agent:", message)
        and app_bot_login is not None
        and isinstance(commit_sha, str)
        and SHA.fullmatch(commit_sha)
        and app_bot_login(commit_sha) is not None
    ):
        return CommitAttribution(commit_sha, VERIFIED_APP_BOT, VERIFIED_APP_BOT, authorship=VERIFIED_APP_BOT)
    if len(trailers) != 1 or "/" not in trailers[0]:
        raise RecordError("author model unknown: missing explicit X-Agent model trailer")
    return _attribute_trailer(commit_sha, trailers[0], repository=repository, task_root=task_root)


def _attribute_trailer(commit_sha: str | None, trailer: str, *, repository: str, task_root: Path) -> CommitAttribution:
    """Resolve one trailer using the existing task/model provenance bindings."""
    if "/" not in trailer:
        raise RecordError("author model unknown: missing explicit X-Agent model trailer")
    harness, model = trailer.split("/", 1)
    if not harness or not model:
        raise RecordError("author model unknown")
    # Task provenance wins over family tokens embedded in the trailer's task id.
    task_file = None
    # Release parsing uses os.devnull when no task store is configured.
    if TASK_ID.fullmatch(model) and task_root != Path(os.devnull):
        try:
            with task_archive_lock(task_root):
                candidate = _hot_or_archived(task_root, f"{model}.json")
                if not candidate.resolve().is_relative_to(task_root.resolve()):
                    raise RecordError("author task provenance unavailable")
                if candidate.exists():
                    task_file = candidate
                    author_task = json_depth.loads(task_file.read_text(encoding="utf-8"), max_depth=_JSON_MAX_DEPTH)
        except (OSError, ValueError) as exc:
            raise RecordError("author task provenance unavailable") from exc
    if task_file is not None:
        # Resolve the recorded model only after validating the task provenance.
        if (
            not isinstance(author_task, dict)
            or not same_repository(author_task.get("repository"), repository)
            or not str(author_task.get("agent") or "").startswith(harness)
        ):
            raise RecordError("author task provenance conflicts with commit trailer")
        if harness.startswith("cursor") and is_cursor_auto_selector(author_task.get("model")):
            author_model = author_task["model"]
        elif harness.startswith("cursor"):
            author_model = (
                author_task.get("resolved_model") if author_task.get("resolved_model_known") is True else None
            )
        else:
            author_model = author_task.get("model")
        family = _author_model_family(harness, str(author_model or ""))
        source = "task-record-archived" if task_file.parent.name == ARCHIVE_DIR_NAME else "task-record"
    else:
        # Require a complete model form; routing's catalog-prefix matching can
        # mistake task ids for models when their task records are missing.
        family = (
            _author_model_family(harness, model)
            if _is_catalog_model_trailer(model) or is_cursor_auto_selector(model)
            else UNKNOWN_AUTHOR_FAMILY
        )
        source = "trailer-model"
        if family in UNRESOLVED_AUTHOR_FAMILIES:
            if not TASK_ID.fullmatch(model):
                raise RecordError("author model unknown")
            # A task id without a task record is unresolved on every harness,
            # a single-family one included: the harness name is author-chosen
            # text, not a model (C-2902 F5).
            family = UNKNOWN_AUTHOR_FAMILY
            source = "unresolved-author"
    if family in UNRESOLVED_AUTHOR_FAMILIES:
        # A committed author without a concrete identity is collected as
        # Unknown (#9944); every review refuses it (require_resolved_branch_authors,
        # C-2902). Unknown is never a reviewer identity.
        family = UNKNOWN_AUTHOR_FAMILY
    has_record = source in {"task-record", "task-record-archived"}
    task_id = model if has_record or (TASK_ID.fullmatch(model) and not _is_catalog_model_trailer(model)) else None
    record_task_id = author_task.get("task_id") if has_record else None
    # Task-record values are JSON, so each slot may hold any type; the filters below keep strings.
    model_values: tuple[object, ...] = (
        (
            author_task.get("model"),
            author_model,
            author_task.get("resolved_model") if author_task.get("resolved_model_known") is True else None,
        )
        if has_record
        else (model,)
        if _is_catalog_model_trailer(model)
        else ()
    )
    if harness.startswith("cursor"):
        model_values = tuple(
            canonical_cursor_model(value) if isinstance(value, str) else value for value in model_values
        )
    author_models = tuple(
        dict.fromkeys(value for value in model_values if isinstance(value, str) and resolve_catalog_model_id(value))
    )
    return CommitAttribution(
        commit_sha if isinstance(commit_sha, str) else None,
        family,
        source,
        normalize_review_task_id(task_id, agent=harness) if task_id is not None else None,
        normalize_review_task_id(record_task_id, agent=harness) if isinstance(record_task_id, str) else None,
        author_models,
    )


def _attribute_commit_authors(
    entry: dict[str, Any],
    *,
    repository: str,
    task_root: Path,
    base_sha: Callable[[], str],
    checkout: Path | None = None,
    unknown_history: Iterable[str] = (),
    app_bot_login: Callable[[str], str | None] | None = None,
    range_shas: frozenset[str] = frozenset(),
) -> tuple[CommitAttribution, ...]:
    """Resolve every declared writer atomically; keep legacy zero/one admission."""
    message = (entry.get("commit") or {}).get("message")
    declared = re.findall(r"(?m)^X-Agent:[^\r\n]*", message) if isinstance(message, str) else []
    if len(declared) <= 1:
        return (
            _attribute_commit(
                entry,
                repository=repository,
                task_root=task_root,
                base_sha=base_sha,
                checkout=checkout,
                unknown_history=unknown_history,
                app_bot_login=app_bot_login,
                range_shas=range_shas,
            ),
        )
    authors = []
    for index, line in enumerate(declared, 1):
        prefix = f"author model unknown: {len(declared)} X-Agent trailers: member {index}"
        match = re.fullmatch(r"X-Agent:[^\S\r\n]*(\S+)[^\S\r\n]*", line)
        if match is None or "/" not in match[1]:
            raise RecordError(f"{prefix} malformed")
        try:
            author = _attribute_trailer(entry.get("sha"), match[1], repository=repository, task_root=task_root)
        except RecordError as exc:
            raise RecordError(f"{prefix}: {exc}") from exc
        if author.family in UNRESOLVED_AUTHOR_FAMILIES or not author.author_models:
            raise RecordError(f"{prefix} unresolved; refusing all authors")
        authors.append(author)
    return tuple(authors)


def author_families(repository: str, pr_number: int, task_root: Path) -> set[str]:
    """Resolve a PR's GitHub-listed commits, including Unknown author families.

    A compatibility reader over :func:`_attribute_commit_authors`. The recorder itself
    uses :func:`pr_review_facts`, which also binds the listing to the local
    ``git rev-list`` enumeration.
    """
    remote = _review_github(repository).active
    commits = _pages(Request("read-commits", repo=remote, number=pr_number))
    if not commits:
        raise RecordError("PR commit set unavailable")
    base: list[str] = []

    def base_sha() -> str:
        if not base:
            pr = _run_json(["gh", "pr", "view", str(pr_number), "--repo", remote, "--json", "baseRefOid"])
            value = pr.get("baseRefOid") if isinstance(pr, dict) else None
            if not isinstance(value, str) or not SHA.fullmatch(value):
                raise RecordError("PR base SHA unavailable; cannot prove clean base merge")
            base.append(value)
        return base[0]

    families = set()
    range_shas = frozenset(entry["sha"] for entry in commits if isinstance(entry.get("sha"), str))
    for entry in commits:
        authors = _attribute_commit_authors(
            entry,
            repository=repository,
            task_root=task_root,
            base_sha=base_sha,
            app_bot_login=listing_app_bot_login(commits, repository),
            range_shas=range_shas,
        )
        families.update(author.family for author in authors if author.family is not None)
    if not families:
        raise RecordError("PR has no attributed author commits")
    return families


# --- complete branch review facts (#9739) ------------------------------------
#
# One calculation serves dispatch admission, reviewer selection and this
# recorder: every commit in ``git rev-list <base>..<head>`` (side parents
# included, no path filter or recent-commit limit), attributed exactly as the
# recorder attributes a commit, plus the incoming writer, plus the protected
# scope the whole branch and the proposed owned paths touch.

BRANCH_FACTS_TIMEOUT_S = 90.0
_GIT_STEP_TIMEOUT_S = 30.0
FACTS_AUTHORSHIP_UNKNOWN = "authorship_unknown"
FACTS_SCOPE_UNKNOWN = "scope_unknown"
FACTS_TARGET_UNKNOWN = "target_unknown"


class BranchFactsError(RecordError):
    """A branch review fact cannot be established; ``code`` names which one."""

    def __init__(self, code: str, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.timed_out = timed_out


@dataclass(frozen=True)
class BranchReviewFacts:
    """Complete authorship and protected scope of one frozen branch target."""

    repository: str
    base_tip_sha: str
    head_sha: str
    merge_base_sha: str | None
    commits: tuple[CommitAttribution, ...]
    existing_families: frozenset[str]
    incoming_writer: str | None
    incoming_family: str | None
    changed_paths: tuple[str, ...]
    owned_paths: tuple[str, ...]
    subject_seats: frozenset[str]
    subject_families: frozenset[str]
    subject_evidence: tuple[str, ...]

    @property
    def unknown_history(self) -> tuple[str, ...]:
        return tuple(sorted(commit.sha for commit in self.commits if commit.source == "unknown_historical"))

    @property
    def app_bot_commits(self) -> tuple[str, ...]:
        """Commits GitHub proved were authored by an allowlisted app bot (#10531)."""
        return tuple(sorted(commit.sha for commit in self.commits if commit.source == VERIFIED_APP_BOT))

    @property
    def author_task_ids(self) -> frozenset[str]:
        """Author task identities retained from the complete branch's X-Agent trailers."""
        return frozenset(
            normalize_review_task_id(task_id)
            for commit in self.commits
            for task_id in (commit.task_id, commit.record_task_id)
            if task_id is not None
        )

    @property
    def author_families(self) -> frozenset[str]:
        """Every committed author family plus the incoming writer's (Cursor Auto stays its own family)."""
        incoming = frozenset({self.incoming_family}) if self.incoming_family else frozenset()
        return self.existing_families | incoming

    @property
    def excluded_families(self) -> frozenset[str]:
        """Every author family excluded from review, including Cursor Auto's own family."""
        if any(commit.source in {"unknown_historical", "suspect_historical"} for commit in self.commits):
            return self.author_families | _VALID_CONCRETE_FAMILIES
        return self.author_families

    @property
    def mixed_family(self) -> bool:
        return is_mixed_family_set(self.excluded_families)

    @property
    def unresolved_authors(self) -> tuple[str, ...]:
        """Commits whose author family is unknown, so no reviewer can be proved outside it.

        Untrailered history a red-team admission receipt froze (``unknown_historical``)
        keeps its own qualification rules and is not counted here.
        """
        return tuple(
            commit.sha or "(unknown commit)"
            for commit in self.commits
            if commit.source == "unresolved-author"
            or (commit.family == UNKNOWN_AUTHOR_FAMILY and commit.source != "unknown_historical")
        )

    @property
    def scope_paths(self) -> tuple[str, ...]:
        """Literal branch changes (both rename sides, deletions) plus the proposed owned paths."""
        return tuple(dict.fromkeys((*self.changed_paths, *self.owned_paths)))

    @property
    def author_models(self) -> tuple[str | None, ...]:
        """Author models per unique commit; an unattributed commit adds None."""
        return tuple(
            model
            for _sha, model in dict.fromkeys(
                (commit.sha, model)
                for commit in self.commits
                # A clean (base or sync) merge authors nothing; a verified app bot has no model.
                if commit.source not in {CLEAN_BASE_MERGE, CLEAN_SYNC_MERGE, VERIFIED_APP_BOT}
                for model in (commit.author_models or (None,))
            )
        )

    @property
    def author_task_models(self) -> tuple[str, ...]:
        """The head commit's task-record author models."""
        return tuple(
            dict.fromkeys(
                model
                for commit in self.commits
                if commit.sha == self.head_sha and commit.source in {"task-record", "task-record-archived"}
                for model in commit.author_models
            )
        )

    def resolver_inputs(
        self, *, risk: str, claude_account: ClaudeAccount, review_profile: str = "code", **overrides: Any
    ) -> ResolverInputs:
        """Reviewer-resolver inputs carrying these facts; ``overrides`` set the remaining fields.

        ``claude_account`` is the account a claude reviewer runs as (#10630); it has no default.
        """
        overrides.setdefault("domain", review_profile)
        overrides.setdefault("author_model", "")
        overrides.setdefault("unknown_history", self.unknown_history)
        overrides.setdefault("app_bot_commits", self.app_bot_commits)
        if self.mixed_family:
            overrides.setdefault("exact_head", self.head_sha)
        return ResolverInputs(
            author_families=self.excluded_families,
            author_commits=tuple((commit.sha, commit.family) for commit in self.commits if commit.sha and commit.family),
            author_models=self.author_models,
            author_task_models=self.author_task_models,
            claude_account=claude_account,
            review_profile=review_profile,
            risk=risk,
            changed_paths=self.changed_paths,
            owned_paths=self.scope_paths,
            subject_seats=self.subject_seats,
            subject_families=self.subject_families,
            subject_evidence=self.subject_evidence,
            repository=self.repository,
            **overrides,
        )

    def reviewer_scope(self, family: str) -> BranchReviewFacts:
        """The opposing family's commits, keeping the complete diff as protected context."""
        try:
            shas = mixed_family_scope(
                ((commit.sha, commit.family) for commit in self.commits if commit.sha and commit.family), family
            )
        except ValueError as exc:
            raise RecordError(str(exc)) from exc
        commits = tuple(commit for commit in self.commits if commit.sha in shas)
        return replace(
            self, commits=commits, existing_families=frozenset(commit.family for commit in commits if commit.family),
            incoming_writer=None, incoming_family=None,
        )

    def receipt(self) -> dict[str, Any]:
        """Privacy-safe summary for a dispatch or verdict record."""
        receipt = {
            "repository": self.repository,
            "base_tip_sha": self.base_tip_sha,
            "head_sha": self.head_sha,
            "commits": len({commit.sha for commit in self.commits}),
            "existing_families": sorted(self.existing_families),
            "incoming_writer": self.incoming_writer,
            "incoming_family": self.incoming_family,
            "author_families": sorted(self.author_families),
            "excluded_families": sorted(self.excluded_families),
            "subject_seats": sorted(self.subject_seats),
            "subject_families": sorted(self.subject_families),
            "scope_paths": len(self.scope_paths),
        }
        if self.unknown_history:
            receipt["unknown_history"] = list(self.unknown_history)
            receipt["authorship"] = "unknown_historical"
        if self.app_bot_commits:
            receipt["app_bot_commits"] = list(self.app_bot_commits)
        return receipt


def _facts_git(
    repo_root: Path, args: list[str], *, deadline: float, code: str, input_bytes: bytes | None = None
) -> bytes:
    """Run one bounded read-only Git step; a timeout or failure is an unknown fact, never a truncated one."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BranchFactsError(code, "branch fact collection timed out; refusing a partial history", timed_out=True)
    # Ignore inherited Git redirection, replacement refs, grafts and commit
    # graphs: the enumeration must describe the repository's real objects.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull})
    command = ["git", "-C", str(repo_root), "-c", "core.commitGraph=false", "-c", "core.hooksPath=/dev/null",
               "--no-replace-objects", *args]
    try:
        proc = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            env=env,
            check=False,
            timeout=min(remaining, _GIT_STEP_TIMEOUT_S),
        )
    except subprocess.TimeoutExpired as exc:
        raise BranchFactsError(code, f"git {args[0]} timed out; refusing a partial history", timed_out=True) from exc
    except OSError as exc:
        raise BranchFactsError(code, f"git {args[0]} unavailable") from exc
    if proc.returncode:
        raise BranchFactsError(code, f"git {args[0]} failed")
    return proc.stdout


def _read_commit_entries(repo_root: Path, shas: list[str], *, deadline: float) -> list[dict[str, Any]]:
    """Read raw commits into the commit-listing shape :func:`_attribute_commit` takes."""
    if not shas:
        return []
    data = _facts_git(
        repo_root,
        ["cat-file", "--batch"],
        deadline=deadline,
        code=FACTS_AUTHORSHIP_UNKNOWN,
        input_bytes="".join(f"{sha}\n" for sha in shas).encode("ascii"),
    )
    entries: list[dict[str, Any]] = []
    position = 0
    try:
        for sha in shas:
            newline = data.index(b"\n", position)
            header = data[position:newline].decode("ascii").split()
            if len(header) != 3 or header[0] != sha or header[1] != "commit":
                raise BranchFactsError(FACTS_AUTHORSHIP_UNKNOWN, f"commit {sha[:12]} unavailable")
            size = int(header[2])
            body = data[newline + 1 : newline + 1 + size].decode("utf-8")
            position = newline + 1 + size + 1
            headers, _, message = body.partition("\n\n")
            # Object headers end at LF; NEL/LS/PS are free text in author names.
            lines = jsonl_lines(headers)
            trees = [line[5:] for line in lines if line.startswith("tree ")]
            parents = [line[7:] for line in lines if line.startswith("parent ")]
            entries.append(
                {
                    "sha": sha,
                    "commit": {"message": message, "tree": {"sha": trees[0] if len(trees) == 1 else None}},
                    "parents": [{"sha": parent} for parent in parents],
                }
            )
    except (ValueError, UnicodeDecodeError) as exc:
        raise BranchFactsError(FACTS_AUTHORSHIP_UNKNOWN, "commit objects unreadable") from exc
    return entries


def incoming_writer_family(agent: str, model: str | None) -> str:
    """The family an incoming writer adds, resolved like a committed ``X-Agent: <agent>/<model>`` trailer.

    Cursor Auto resolves to its own Cursor family. Raises ``BranchFactsError``
    when the family is unknown.
    """
    harness = str(agent or "").strip().lower()
    concrete = str(model or "").strip()
    family = _author_model_family(harness, concrete)
    if (family in UNRESOLVED_AUTHOR_FAMILIES or family == "unknown") and harness in SINGLE_FAMILY_HARNESSES:
        family = SINGLE_FAMILY_HARNESSES[harness]
    if family in UNRESOLVED_AUTHOR_FAMILIES or family == "unknown":
        raise BranchFactsError(
            FACTS_AUTHORSHIP_UNKNOWN, f"incoming writer family unknown ({harness}/{concrete or '(default)'})"
        )
    return family


def collect_branch_review_facts(
    *,
    repository: str,
    repo_root: Path,
    base_tip_sha: str,
    head_sha: str,
    task_root: Path,
    incoming_agent: str | None = None,
    incoming_model: str | None = None,
    owned_paths: Iterable[str] = (),
    subject_seats: Iterable[str] = (),
    subject_families: Iterable[str] = (),
    timeout_s: float = BRANCH_FACTS_TIMEOUT_S,
    authorship_exclude_sha: str | None = None,
    unknown_history: Iterable[str] = (),
    app_bot_login: Callable[[str], str | None] | None = None,
) -> BranchReviewFacts:
    """Collect complete authorship and protected scope for ``base_tip_sha..head_sha``.

    Commit membership is ``git rev-list <base>..<head>`` (A1), attributed by the
    recorder's own per-commit rules (trailer, hot or archived task record,
    Cursor attestation, single-family harness, Git-proven clean base or sync
    merge, GitHub-proven app bot).
    ``incoming_agent``/``incoming_model`` name the writer about to be
    dispatched, after any substitution. Scope is the literal diff against the
    merge-base (both rename sides and deletions) plus ``owned_paths``, with the
    known subject seats ``prepare_subject_exclusion`` derives from it; shared
    or missing subject metadata remains unknown. A fresh branch
    passes ``head_sha == base_tip_sha``. Admission may exclude commits reachable
    from a frozen main/rebase tip with ``authorship_exclude_sha`` (#9988); this
    never narrows the diff or protected scope against the original base.
    Reads only; never fetches Git objects. An untrailered commit that no other
    rule attributes is checked with ``app_bot_login`` (default: a live GitHub
    read of that SHA, :func:`live_app_bot_login`). Raises
    ``BranchFactsError`` (``code`` = authorship, scope or target unknown).
    """
    deadline = time.monotonic() + timeout_s
    endpoints = [("base", base_tip_sha), ("head", head_sha)]
    if authorship_exclude_sha is not None:
        endpoints.append(("authorship exclusion", authorship_exclude_sha))
    for label, sha in endpoints:
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise BranchFactsError(FACTS_TARGET_UNKNOWN, f"{label} SHA missing or invalid")
        try:
            _facts_git(repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"], deadline=deadline, code=FACTS_TARGET_UNKNOWN)
        except BranchFactsError as exc:
            if exc.timed_out:
                raise
            raise BranchFactsError(
                FACTS_TARGET_UNKNOWN, f"{label} commit {sha[:12]} not available locally; fetch and retry"
            ) from exc
    revisions = ["rev-list", f"{base_tip_sha}..{head_sha}"]
    if authorship_exclude_sha is not None:
        revisions.append(f"^{authorship_exclude_sha}")
    listed = _facts_git(repo_root, revisions, deadline=deadline, code=FACTS_AUTHORSHIP_UNKNOWN)
    shas = listed.decode("ascii", errors="strict").split()
    commits: list[CommitAttribution] = []
    unknown_history = frozenset(unknown_history)
    app_bot_login = app_bot_login or partial(live_app_bot_login, repository)
    range_shas = frozenset(shas)
    for entry in _read_commit_entries(repo_root, shas, deadline=deadline):
        try:
            commits.extend(
                _attribute_commit_authors(
                    entry,
                    repository=repository,
                    task_root=task_root,
                    # Clean main merges still need the existing object/tree
                    # proof, bound to the frozen main tip rather than a stale base.
                    base_sha=lambda: authorship_exclude_sha or base_tip_sha,
                    checkout=repo_root,
                    unknown_history=unknown_history,
                    app_bot_login=app_bot_login,
                    range_shas=range_shas,
                )
            )
        except RecordError as exc:
            raise BranchFactsError(FACTS_AUTHORSHIP_UNKNOWN, f"commit {entry['sha'][:12]}: {exc}") from exc
        if time.monotonic() > deadline:
            raise BranchFactsError(
                FACTS_AUTHORSHIP_UNKNOWN, "branch fact collection timed out; refusing a partial history"
            )
    # A proven clean merge of the excluded tip authors nothing, and it stays
    # in the facts. Dispatch can still admit the incoming writer. A branch
    # review refuses that range in refuse_excluded_only_range.
    incoming_writer = None
    incoming_family = None
    if incoming_agent:
        incoming_writer = f"{incoming_agent}/{incoming_model or '(default)'}"
        incoming_family = incoming_writer_family(incoming_agent, incoming_model)
    merge_base = None
    changed: tuple[str, ...] = ()
    if base_tip_sha != head_sha:
        merge_base = (
            _facts_git(repo_root, ["merge-base", base_tip_sha, head_sha], deadline=deadline, code=FACTS_SCOPE_UNKNOWN)
            .decode("ascii")
            .strip()
        )
        try:
            changed = git_changed_paths(repo_root, merge_base, head_sha)
        except TargetResolutionError as exc:
            raise BranchFactsError(FACTS_SCOPE_UNKNOWN, str(exc)) from exc
    owned = tuple(str(path) for path in owned_paths)
    prepared = prepare_subject_exclusion(
        subject_seats=frozenset(subject_seats),
        subject_families=frozenset(subject_families),
        owned_paths=tuple(dict.fromkeys((*changed, *owned))),
        repository=repository,
    )
    if prepared.fail_closed_reason:
        raise BranchFactsError(FACTS_SCOPE_UNKNOWN, prepared.fail_closed_reason)
    return BranchReviewFacts(
        repository=repository,
        base_tip_sha=base_tip_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base,
        commits=tuple(commits),
        existing_families=frozenset(commit.family for commit in commits if commit.family),
        incoming_writer=incoming_writer,
        incoming_family=incoming_family,
        changed_paths=changed,
        owned_paths=owned,
        subject_seats=prepared.seats,
        subject_families=prepared.families,
        subject_evidence=prepared.evidence,
    )


def structural_review_route(
    facts: BranchReviewFacts,
    *,
    risk: str,
    claude_account: ClaudeAccount,
    review_profile: str = "code",
    review_mode: str = "cross_family",
) -> ReviewerResolution:
    """Resolve independent review, including the mixed-family pair, under the live catalog floors.

    Structural only (A2): no routing snapshot, so health and quota never decide
    feasibility; the resolver's own health semantics stay unchanged.
    ``claude_account`` is the account a claude reviewer would run as: an
    unresolved one removes the claude seats (#10630).
    """
    inputs = facts.resolver_inputs(
        risk=risk, review_profile=review_profile, claude_account=claude_account, structural=True
    )
    if review_mode == "red_team" and facts.incoming_writer:
        # This is the declared new writer, never an assumed historical model.
        inputs = replace(inputs, author_models=(*inputs.author_models, facts.incoming_writer))
    return resolve_reviewer(inputs, review_mode=review_mode)


def _canonical_base_branch_name(base_branch: str) -> str:
    """Branch name behind the spellings target resolution already accepts.

    ``main``, ``origin/main``, ``remotes/origin/main``,
    ``refs/remotes/origin/main`` and ``refs/heads/main`` name one branch.
    A non-default ref keeps its own name, including a slash that is part of it.
    """
    name = base_branch.strip()
    for prefix in ("refs/remotes/origin/", "remotes/origin/", "refs/heads/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def authorship_exclude_sha(repo_root: Path, *, base_branch: str | None) -> str | None:
    """Current default-branch tip when ``base_branch`` names that branch.

    The same rule as dispatch admission (#9988): a review whose base is the
    default branch drops commits reachable from that branch's current tip.
    Any other base keeps the full enumeration. The tip is the local
    ``refs/remotes/origin/HEAD``. A checkout that has not recorded it returns
    None, so callers keep the previous enumeration rather than guessing a tip.
    Qualified spellings of that ref are reduced to the branch name first.
    """
    if not isinstance(base_branch, str) or not base_branch.strip():
        return None
    name = _canonical_base_branch_name(base_branch)
    try:
        ref = _facts_git(
            repo_root,
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            deadline=time.monotonic() + 30,
            code=FACTS_TARGET_UNKNOWN,
        )
    except BranchFactsError:
        return None
    pointed = ref.decode("ascii", errors="strict").strip()
    if not pointed.startswith("origin/"):
        return None
    if name != pointed.removeprefix("origin/"):
        return None
    try:
        tip = _facts_git(
            repo_root,
            ["rev-parse", f"{pointed}^{{commit}}"],
            deadline=time.monotonic() + 30,
            code=FACTS_TARGET_UNKNOWN,
        )
    except BranchFactsError:
        return None
    sha = tip.decode("ascii", errors="strict").strip()
    if not SHA.fullmatch(sha):
        return None
    return sha


def default_branch_review_base(repo_root: Path, head_sha: str, *, fetch: bool = True) -> tuple[str, str]:
    """The fetched default-branch tip and its merge-base with ``head_sha`` (C-2902 F6).

    Every branch review is attributed over ``merge-base..head`` with that tip
    excluded, at dispatch and at record. The default branch is the one origin
    advertises as ``HEAD``, fetched fresh into ``origin/<default>``. A caller's
    ``--base`` and the PR's base are never read: the author chooses both. Raises
    ``BranchFactsError`` when the remote, the fetch or a shared history is unavailable.
    ``fetch=False`` reads the existing ``origin/<default>`` without writing any ref
    or object, for read-only callers such as ``budget-check``.
    """
    from lu_machinery.common.git_context import UnsafeBranchNameError, validate_plain_branch_name

    deadline = time.monotonic() + BRANCH_FACTS_TIMEOUT_S
    advertised = _facts_git(
        repo_root, ["ls-remote", "--symref", "origin", "HEAD"], deadline=deadline, code=FACTS_TARGET_UNKNOWN
    ).decode("utf-8", errors="replace")
    match = re.search(r"(?m)^ref: refs/heads/(\S+)\tHEAD$", advertised)
    try:
        name = validate_plain_branch_name(match.group(1), repo_root=repo_root) if match else None
    except UnsafeBranchNameError:
        name = None
    if name is None:
        raise BranchFactsError(FACTS_TARGET_UNKNOWN, "origin advertises no default branch")
    tracking = f"refs/remotes/origin/{name}"
    if fetch:
        _facts_git(
            repo_root,
            ["fetch", "--no-tags", "--no-write-fetch-head", "origin", f"+refs/heads/{name}:{tracking}"],
            deadline=deadline,
            code=FACTS_TARGET_UNKNOWN,
        )
    tip = (
        _facts_git(
            repo_root, ["rev-parse", "--verify", f"{tracking}^{{commit}}"], deadline=deadline, code=FACTS_TARGET_UNKNOWN
        )
        .decode("ascii", errors="replace")
        .strip()
    )
    if not SHA.fullmatch(tip) or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise BranchFactsError(FACTS_TARGET_UNKNOWN, "default-branch tip or head SHA invalid")
    try:
        merge_base = _facts_git(repo_root, ["merge-base", tip, head_sha], deadline=deadline, code=FACTS_TARGET_UNKNOWN)
    except BranchFactsError as exc:
        if exc.timed_out:
            raise
        raise BranchFactsError(
            FACTS_TARGET_UNKNOWN, f"head {head_sha[:12]} shares no available history with origin/{name}"
        ) from exc
    return tip, merge_base.decode("ascii", errors="replace").strip()


def _shas_not_reachable_from(repo_root: Path, shas: list[str], exclude_sha: str | None) -> list[str]:
    """Drop commits reachable from ``exclude_sha``, including that commit.

    One ``git rev-list --no-walk`` covers every valid SHA under a single
    deadline. Strings that are not commit SHAs stay, without starting Git.
    """
    if exclude_sha is None:
        return list(shas)
    pending = [sha for sha in shas if isinstance(sha, str) and SHA.fullmatch(sha)]
    if not pending:
        return [sha if isinstance(sha, str) else "" for sha in shas]
    listed = _facts_git(
        repo_root,
        ["rev-list", "--no-walk", "--stdin"],
        deadline=time.monotonic() + _GIT_STEP_TIMEOUT_S,
        code=FACTS_AUTHORSHIP_UNKNOWN,
        input_bytes=("\n".join((*pending, f"^{exclude_sha}")) + "\n").encode("ascii"),
    )
    reachable = set(listed.decode("ascii", errors="strict").split())
    kept: list[str] = []
    for sha in shas:
        if isinstance(sha, str) and SHA.fullmatch(sha):
            if sha in reachable:
                kept.append(sha)
        else:
            kept.append(sha if isinstance(sha, str) else "")
    return kept


def refuse_excluded_only_range(facts: BranchReviewFacts, *, repo_root: Path, exclude_sha: str | None) -> None:
    """Refuse a branch review that has no commit of its own.

    Fact collection stays quiet. Dispatch admits a known incoming writer onto
    a branch that has not authored a commit yet, including after a clean merge
    of the excluded tip, and a rebase plan still enumerates that range. The
    recorder and the resolver still refuse it: the frozen base and head are
    the same commit, every remaining commit is reachable from the excluded
    tip, or the only commits left are proven clean merges of it.
    """
    if facts.base_tip_sha == facts.head_sha:
        raise BranchFactsError(
            FACTS_AUTHORSHIP_UNKNOWN,
            "empty review range: the branch has no commits of its own",
        )
    if exclude_sha is None or facts.existing_families:
        return
    if any(commit.family is not None for commit in facts.commits):
        return
    if facts.commits:
        raise BranchFactsError(
            FACTS_AUTHORSHIP_UNKNOWN,
            "author model unknown: missing explicit X-Agent model trailer",
        )
    full = _facts_git(
        repo_root,
        ["rev-list", f"{facts.base_tip_sha}..{facts.head_sha}"],
        deadline=time.monotonic() + 30,
        code=FACTS_AUTHORSHIP_UNKNOWN,
    )
    if full.strip():
        raise BranchFactsError(
            FACTS_AUTHORSHIP_UNKNOWN,
            "author model unknown: missing explicit X-Agent model trailer",
        )


def pr_review_facts(
    repository: str,
    pr_number: int,
    *,
    head_sha: str,
    task_root: Path,
    repo_root: Path,
    subject_seats: Iterable[str] = (),
    subject_families: Iterable[str] = (),
    unknown_history: Iterable[str] = (),
    fetch: bool = True,
    current_head: bool = True,
    github_repository: str | None = None,
) -> BranchReviewFacts:
    """Facts for an open PR's frozen head, bound to GitHub's listing of its commits (A1).

    ``repository`` names the records the facts are matched against; GitHub is read at
    ``github_repository`` (a dual-slug row's active slug), else at ``repository``.

    The head comes from GitHub; the range is the fetched default branch to
    that head (:func:`default_branch_review_base`), never the PR's base, which
    the author can retarget. Membership comes from the local ``git rev-list``.
    Any difference from GitHub's listing refuses, so a PR whose base is not the
    default branch is refused rather than attributed over a shorter range.
    With ``current_head=False`` the facts are those of an earlier head the PR
    still contains: every commit of its range must be in GitHub's listing.
    """
    remote = github_repository or repository
    pr = _run_json(["gh", "pr", "view", str(pr_number), "--repo", remote, "--json", "headRefOid"])
    head = pr.get("headRefOid") if isinstance(pr, dict) else None
    if current_head and head != head_sha:
        raise RecordError("PR head moved since review; re-run exact-head review")
    listed = _pages(Request("read-commits", repo=remote, number=pr_number))
    if not listed:
        raise RecordError("PR commit set unavailable")
    github_shas = [entry.get("sha") for entry in listed]
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in github_shas):
        raise RecordError("PR commit set malformed")
    exclude, base = default_branch_review_base(repo_root, head_sha, fetch=fetch)
    facts = collect_branch_review_facts(
        repository=repository,
        repo_root=repo_root,
        base_tip_sha=base,
        head_sha=head_sha,
        task_root=task_root,
        subject_seats=subject_seats,
        subject_families=subject_families,
        authorship_exclude_sha=exclude,
        unknown_history=unknown_history,
        # GitHub's live listing of this PR carries each commit's author proof.
        app_bot_login=listing_app_bot_login(listed, repository),
    )
    refuse_excluded_only_range(facts, repo_root=repo_root, exclude_sha=exclude)
    if not current_head:
        # Commits pushed after the reviewed head need not be local; the reviewed range must be listed.
        if head_sha not in github_shas or not {commit.sha or "" for commit in facts.commits} <= set(github_shas):
            raise RecordError("PR no longer contains the reviewed head's commits")
        return facts
    differs = RecordError(
        "PR commit set differs from the local base..head enumeration from the default branch; "
        "target the default branch, or fetch and retry"
    )
    try:
        github_own = _shas_not_reachable_from(repo_root, github_shas, exclude)
    except BranchFactsError as exc:
        # A listed commit missing locally is a difference, not a partial read; a timeout stays one.
        if exc.timed_out:
            raise
        raise differs from exc
    if sorted(github_own) != sorted({commit.sha or "" for commit in facts.commits}):
        raise differs
    return facts


def admitted_unknown_history(
    *,
    repository: str,
    branch: str,
    head_sha: str,
    task_root: Path,
    repo_root: Path,
) -> tuple[str, ...]:
    """Read existing authoring receipts, never derive allowances from the review head.

    Both the frozen remote head and each allowed commit must precede the
    admission head, which must precede this review of the same branch.
    """
    allowed: set[str] = set()
    deadline = time.monotonic() + BRANCH_FACTS_TIMEOUT_S
    receipts = []
    try:
        with task_archive_lock(task_root):
            paths = (*task_root.glob("*.json"), *(task_root / ARCHIVE_DIR_NAME).glob("*.json"))
            for path in paths:
                try:
                    task = read_listed_task_record(path, tasks_dir=task_root)
                except TaskIdentityMismatch as exc:
                    # An exact copy of the named record adds nothing; any other misnamed file refuses.
                    if is_identical_copy(exc):
                        continue
                    raise BranchFactsError(
                        FACTS_AUTHORSHIP_UNKNOWN, f"task record {path} unreadable; {misnamed_record_refusal(path)}"
                    ) from exc
                except (OSError, ValueError) as exc:
                    raise BranchFactsError(FACTS_AUTHORSHIP_UNKNOWN, f"task record {path} unreadable") from exc
                if not isinstance(task, dict) or task.get("status") == "dry_run":
                    continue
                receipt = task.get("authoring_review_admission")
                if (
                    not isinstance(receipt, dict)
                    or not same_repository(receipt.get("repository"), repository)
                    or receipt.get("head_branch") != branch
                ):
                    continue
                if receipt.get("review_plan") != "red_team" or not receipt.get("unknown_history") or receipt.get("refusal"):
                    continue
                history = receipt["unknown_history"]
                admitted = receipt.get("head_sha")
                remote = receipt.get("history_remote_head_sha")
                reviewer = receipt.get("reviewer")
                if (
                    receipt.get("check") != "structural"
                    or receipt.get("applicable") is not True
                    or "assumed_model" not in receipt
                    or receipt["assumed_model"] is not None
                    or not isinstance(reviewer, dict)
                    or reviewer.get("review_mode") != "red_team"
                    or not isinstance(history, list)
                    or not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in history)
                    or not isinstance(admitted, str)
                    or not SHA.fullmatch(admitted)
                    or not isinstance(remote, str)
                    or not SHA.fullmatch(remote)
                ):
                    raise BranchFactsError(FACTS_AUTHORSHIP_UNKNOWN, "historical authorship admission receipt malformed")
                receipts.append((history, admitted, remote))
    except OSError as exc:
        raise BranchFactsError(FACTS_AUTHORSHIP_UNKNOWN, "RED_TEAM_REFUSED: task ledger unavailable") from exc
    for history, admitted, remote in receipts:
        ancestors = set(
            _facts_git(
                repo_root,
                ["rev-list", head_sha],
                deadline=deadline,
                code=FACTS_AUTHORSHIP_UNKNOWN,
            )
            .decode("ascii")
            .split()
        )
        if admitted not in ancestors:
            continue
        admitted_ancestors = set(
            _facts_git(
                repo_root,
                ["rev-list", admitted],
                deadline=deadline,
                code=FACTS_AUTHORSHIP_UNKNOWN,
            )
            .decode("ascii")
            .split()
        )
        remote_ancestors = set(
            _facts_git(
                repo_root,
                ["rev-list", remote],
                deadline=deadline,
                code=FACTS_AUTHORSHIP_UNKNOWN,
            )
            .decode("ascii")
            .split()
        )
        if remote not in admitted_ancestors or not set(history) <= remote_ancestors:
            raise BranchFactsError(
                FACTS_AUTHORSHIP_UNKNOWN, "historical authorship admission receipt conflicts with history"
            )
        allowed.update(history)
    return tuple(sorted(allowed))


def _task_flag_values(task: dict[str, Any], key: str) -> tuple[str, ...]:
    value = task.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RecordError(f"review task {key} malformed")
    return tuple(value)


def _review_candidates(task: Mapping[str, Any], model: str) -> list[Any]:
    """The catalog review candidates of the reviewer's route and model."""
    # Seat aliases (``grok-build``) name the canonical route the catalog lists.
    agent = normalize_seat(str(task.get("agent") or "")) or ""
    canonical = resolve_catalog_model_id(model)
    return [
        candidate
        for candidate in REVIEW_CANDIDATES.values()
        if candidate.route == agent and resolve_catalog_model_id(candidate.concrete_model) == canonical
    ]


def _critical_review(task: Mapping[str, Any], facts: BranchReviewFacts) -> bool:
    """A review whose approval is a security sign-off: the security profile, critical risk or a security path."""
    profile = str(task.get("review_profile") or "code").strip().casefold()
    risk = str(task.get("review_risk") or "").strip().casefold()
    return profile == "security" or risk == "critical" or is_security_sensitive_change(facts.changed_paths)


def _require_qualified_reviewer(
    facts: BranchReviewFacts, *, task: dict[str, Any], model: str, family: str, review_mode: str = "cross_family",
    low_tier: bool = False,
) -> None:
    """Evaluate the actual reviewer against the resolver's rules on the branch facts (AC-01).

    Protected seats and the risk floor bind as in selection; the reviewer need
    not be the resolver's current top choice, so quota or ladder order alone
    never invalidates a valid unchanged-head approval. The planned risk is the
    review's recorded ``review_risk`` (the resolver default when absent);
    path inference only raises it.
    """
    profile = str(task.get("review_profile") or "code").strip().casefold()
    risk = str(task.get("review_risk") or "medium")
    # ``security`` is not a catalog profile. Qualifying it as critical infra
    # keeps the sign-off seats and refuses every other seat. The marker still
    # records code, and the findings stay withheld.
    if profile == "security":
        seat_profile, risk = "infra", "critical"
    else:
        seat_profile = profile
    agent = normalize_seat(str(task.get("agent") or "")) or ""
    # Lane flags and task-family aliases classify a review as Ukrainian even
    # when the profile field says code. The seat allowlist covers that too.
    if is_ukrainian_review(task) and (agent, family) not in UKRAINIAN_SEAT_ROUTES:
        raise RecordError("reviewer not qualified: Ukrainian review needs a Gemini or Sol seat")
    candidates = _review_candidates(task, model)
    if not candidates:
        raise RecordError(f"reviewer not qualified: {agent}/{model} is not a catalog review candidate")
    if profile == "ukrainian":
        if facts.subject_seats or facts.subject_families or is_security_sensitive_change(facts.changed_paths):
            raise RecordError("reviewer not qualified: protected scope needs a code-profile review")
        return
    inputs = facts.resolver_inputs(
        risk=risk,
        review_profile=seat_profile,
        author_model=task.get("review_author_model") or "" if review_mode == "red_team" else "",
        # The reviewer ran as the account its own task record names (#10630); no quota decides a finished review.
        claude_account=account_from_record(task, agent=agent),
        structural=True,
    )
    if low_tier:
        # Only family independence is waived. The original facts still prove
        # non-authorship; model, risk, subject and harness qualification bind.
        inputs = replace(inputs, author_families=frozenset(), author_commits=(), author_model="", author_family=None)
    if review_mode == "cross_family" and mixed_family_review(inputs):
        facts.reviewer_scope(family)
    results = [evaluate_candidate(candidate, inputs, review_mode=review_mode) for candidate in candidates]
    if not any(result.status == "eligible" for result in results):
        reasons = "; ".join(sorted({str(result.reason) for result in results}))
        raise RecordError(f"reviewer not qualified for this branch: {reasons}")


def _repo_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        raise RecordError("Git repository lookup timed out after 30 seconds") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RecordError("Git repository lookup failed") from exc
    return Path(result.stdout.strip()).resolve().parent


@contextmanager
def pr_lock(repository: str, number: int, lock_root: Path):
    """Serialize publication on one PR across its heads, so the comments a poster checked still hold."""
    repositories = {repository}
    try:
        aliases = repository_slugs(repository)
        identity = repository_identity(repository) if aliases else None
    except FleetRepoError as exc:
        raise RecordError("fleet repository identity is unavailable") from exc
    if isinstance(identity, int):
        # An older recorder still locks the slug it knows. Hold those files in the same order.
        repositories.update(aliases)
        repositories.add(str(identity))
    lock_root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        for spelling in sorted(repositories):
            name = hashlib.sha256(f"{spelling}\0pr\0{number}".encode()).hexdigest() + ".lock"
            lock_file = locks.enter_context((lock_root / name).open("a+"))
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def _coverage_tokens(
    denominator: Iterable[str] | None,
    coverage: Iterable[str] | None,
    void: str | None,
) -> tuple[str, str, str] | None:
    """Marker tokens for a chunk, or None when this verdict declares no item denominator."""
    if denominator is None and coverage is None and void is None:
        return None
    if denominator is None or coverage is None:
        raise RecordError("item coverage requires both a denominator and a coverage set")
    if void is not None and void not in CHUNK_VOIDS:
        raise RecordError("unknown chunk void reason")
    declared = tuple(denominator)
    covered = tuple(coverage)
    if not declared or declared != tuple(sorted(set(declared))):
        raise RecordError("denominator must be sorted, unique and non-empty")
    if any(ITEM_ID_RE.fullmatch(item) is None for item in declared):
        raise RecordError("item id is not a marker token")
    if void is not None:
        if covered:
            raise RecordError("a voided chunk covers no items")
        return ",".join(declared), "-", void
    if covered != tuple(sorted(set(covered))) or not set(covered) <= set(declared):
        raise RecordError("coverage must be a sorted unique subset of the denominator")
    if not covered:
        raise RecordError("a chunk verdict with no covered items needs a void reason")
    if any(ITEM_ID_RE.fullmatch(item) is None for item in covered):
        raise RecordError("item id is not a marker token")
    return ",".join(declared), ",".join(covered), ""


def _withheld_comment(fields: Mapping[str, Any], finding_count: int | Literal["unknown"]) -> str:
    """The comment already decided, with the reply omitted and the finding count kept."""
    return build_comment(
        sha=fields["sha"],
        task_id=fields["task_id"],
        started=fields["started"],
        verdict=fields["verdict"],
        model=fields["model"],
        family=fields["family"],
        reply="",
        review_mode=fields["review_mode"],
        review_profile=fields["review_profile"],
        sources_mcp_call_count=fields["sources_mcp_call_count"],
        unknown_history=fields["unknown_history"],
        withheld_findings=finding_count,
        app_bot=fields["app_bot"],
        score=fields["score"],
        author_models=fields["author_models"],
        denominator=fields["denominator"],
        coverage=fields["coverage"],
        void=fields["void"],
        residuals=fields["residuals"],
        scope_commits=fields["scope_commits"],
        second_review=fields["second_review"],
    )


def build_comment(
    *,
    sha: str,
    task_id: str,
    started: str,
    verdict: str,
    model: str,
    family: str,
    reply: str,
    review_mode: str = "cross_family",
    review_profile: str = "code",
    sources_mcp_call_count: int | None = None,
    unknown_history: Iterable[str] = (),
    withheld_findings: int | Literal["unknown"] | None = None,
    app_bot: bool = False,
    score: int | None = None,
    author_models: Iterable[str | None] = (),
    denominator: Iterable[str] | None = None,
    coverage: Iterable[str] | None = None,
    void: str | None = None,
    residuals: Iterable[str] | None = None,
    repair_withheld: bool = False,
    scope_commits: Iterable[str] | None = None,
    second_review: bool = False,
) -> str:
    if not SHA.fullmatch(sha):
        raise RecordError("reviewed SHA must be a full 40-hex commit SHA")
    if review_mode not in {"cross_family", "red_team"}:
        raise RecordError("unsupported review mode")
    reply = reply.replace(MARKER_PREFIX, "&lt;" + MARKER_PREFIX[1:])
    if MARKER_PREFIX in reply:
        raise RecordError("review reply contains reserved verdict marker")
    mode_field = " review_mode=red_team" if review_mode == "red_team" else ""
    history = tuple(sorted(set(unknown_history)))
    if history and (review_mode != "red_team" or not all(SHA.fullmatch(sha) for sha in history)):
        raise RecordError("unknown history requires red_team and full commit SHAs")
    history_field = f" unknown_history={','.join(history)}" if history else ""
    authorship_field = f" authorship={VERIFIED_APP_BOT}" if app_bot else ""
    ukrainian = is_ukrainian_review({"review_profile": review_profile})
    _require_sources_mcp_calls({"review_profile": review_profile, "sources_mcp_call_count": sources_mcp_call_count})
    profile_field = " review_profile=ukrainian" if ukrainian else ""
    sources_field = f" sources_calls={sources_mcp_call_count}" if sources_mcp_call_count is not None else ""
    tokens = _coverage_tokens(denominator, coverage, void)
    coverage_field = ""
    coverage_lines = ""
    if tokens is not None:
        declared_token, covered_token, void_token = tokens
        coverage_field = f" denominator={declared_token} coverage={covered_token}"
        coverage_lines = f"Denominator: {declared_token}\nCoverage: {covered_token}\n"
        if void_token:
            coverage_field += f" void={void_token}"
            coverage_lines += f"Chunk void: {void_token}\n"
    # The score is advisory (C-3020): stored for calibration, never read by a gate.
    score_text = f"{score}/10" if score is not None else "absent"
    authors = sorted({author for author in author_models if author})
    pair_field = pair_lines = ""
    if scope_commits is not None:
        scope = tuple(sorted(set(scope_commits)))
        pair_families = attested_author_families([{"author_models": authors}])
        if (
            not is_mixed_family_set(pair_families) or family not in pair_families
            or not scope or any(not SHA.fullmatch(commit) for commit in scope)
            or review_mode != "cross_family" or ukrainian or tokens is not None
        ):
            raise RecordError("invalid mixed-family review scope")
        scope_token = ",".join(scope)
        # The token names the pair's sorted author families, so an Anthropic/OpenAI pair keeps its old wire value.
        pair_token = "_".join(sorted(pair_families))
        other = ",".join(sorted(pair_families - {family}))
        pair_field = f" review_pair={pair_token} scope_commits={scope_token}"
        pair_lines = f"Review pair: {pair_token}\nScope author family: {other}\nScope commits: {scope_token}\n"
    second_field = second_line = ""
    if second_review:
        if scope_commits is not None or review_mode != "cross_family" or ukrainian or tokens is not None:
            raise RecordError("a second review is a whole-head cross-family code/infra record")
        second_field = f" second_review={SECOND_REVIEW_TOKEN}"
        second_line = f"{SECOND_REVIEW_LINE}\n"
    marker = (
        f"<!-- cf-verdict v1 sha={sha} task={task_id} started={started} verdict={verdict} "
        f"model={model} family={family}{mode_field}{history_field}{authorship_field}{profile_field}{sources_field}"
        f"{coverage_field}{pair_field}{second_field} -->"
    )
    prefix = (
        ("### Adversarial red-team review\n" if review_mode == "red_team" else "### Cross-family review\n")
        + ("Review mode: red_team\n" if review_mode == "red_team" else "")
        + (
            f"Historical authorship: unknown_historical\nUnknown history: {','.join(history)}\nNo author model assumed.\n"
            if history
            else ""
        )
        + (f"Authorship: {VERIFIED_APP_BOT}\n" + ("" if history else "No author model assumed.\n") if app_bot else "")
        + ("Review profile: ukrainian\n" if ukrainian else "")
        + (f"Sources MCP calls: {sources_mcp_call_count}\n" if sources_mcp_call_count is not None else "")
        + f"head: {sha}\n"
        f"Reviewer family: {family}\n"
        f"VERDICT: {verdict}\n"
        f"Reviewer model: {model}\n"
        f"Review score: {score_text}\n"
        + (f"Authored by: {', '.join(authors)}\n" if authors else "")
        + f"Task id: {task_id}\n"
        + coverage_lines
        + pair_lines
        + second_line
        + "\n"
    )
    residual_ids = tuple(residuals or ())
    if any(_RESIDUAL_ID.fullmatch(item) is None for item in residual_ids):
        raise RecordError("residual finding id is not a marker token")
    # Listed after the reply so the record prefix stays the marker's fixed lines.
    residual_line = ""
    if residual_ids:
        residual_line = "Residuals: " + ", ".join(f"{item} (P3)" for item in residual_ids) + "\n\n"
    if withheld_findings is not None:
        return prefix + f"Withheld findings: {withheld_findings}\n" + "Findings are held privately.\n\n" + marker
    prefix += "<details><summary>Reviewer's reply</summary>\n\n"
    repair_note = f"{REPAIR_WITHHELD}\n\n" if repair_withheld else ""
    suffix = f"\n\n</details>\n\n{residual_line}{repair_note}{marker}"
    available = MAX_COMMENT_BYTES - len((prefix + suffix).encode())
    if available < 0:
        raise RecordError("comment metadata exceeds GitHub limit")
    if len(reply.encode()) > available:
        flag = f"\n\n{TRUNCATION_FLAG}"
        available -= len(flag.encode())
        if available < 0:
            raise RecordError("comment metadata exceeds GitHub limit")
        reply = reply.encode()[:available].decode("utf-8", errors="ignore") + flag
    return prefix + reply + suffix


def _reviewed_tree_entries(primary_root: Path, sha: str, directory: PurePosixPath) -> dict[str, str]:
    """Git mode of each entry in one directory of the reviewed commit; empty when unreadable."""
    listed = [] if directory == PurePosixPath(".") else ["--", f"{directory}/"]
    try:
        output = _facts_git(
            primary_root,
            ["--literal-pathspecs", "ls-tree", "-z", sha, *listed],
            deadline=time.monotonic() + _GIT_STEP_TIMEOUT_S,
            code=FACTS_TARGET_UNKNOWN,
        )
    except BranchFactsError:
        return {}
    entries = {}
    for entry in output.decode("utf-8", errors="surrogateescape").split("\0"):
        meta, tab, name = entry.partition("\t")
        if tab:
            entries[name] = meta.split(" ", 1)[0]
    return entries


def _removed_checkout_root(root: Path, primary_root: Path, sha: object) -> bool:
    """Whether ``root`` is a reviewed checkout that no longer exists, recorded inside the primary checkout.

    The dispatcher removes a read-only reviewer's worktree when its task settles,
    usually before the verdict is recorded. Its citations are then checked
    against the reviewed commit, which is what that worktree held.
    """
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or ".." in root.parts or os.path.lexists(root):
        return False
    resolved_primary = primary_root.resolve(strict=True)
    # Existing ancestors resolve, so a symlinked parent cannot carry the root outside.
    resolved_root = root.resolve()
    return resolved_root != resolved_primary and resolved_root.is_relative_to(resolved_primary)


def repository_relative_reply(reply: str, *, task: dict[str, Any], primary_root: Path) -> str:
    """Rewrite checkout citations on scanner-stable lines; scan the final text."""
    checkout = task.get("worktree_path")
    roots = [primary_root]
    if isinstance(checkout, str) and Path(checkout).is_absolute():
        roots.insert(0, Path(checkout))
    reviewed = task.get("worktree_base_sha")

    def removed_checkout_citation(cited: PurePosixPath, suffix: str) -> bool:
        if cited == PurePosixPath("."):
            return not suffix
        entries = _reviewed_tree_entries(primary_root, reviewed, cited.parent)
        mode = entries.get(cited.as_posix())
        if suffix:
            # A file literally named with the annotation would make the citation ambiguous.
            return mode in {"100644", "100755"} and f"{cited}{suffix}" not in entries
        return mode in {"100644", "100755", "120000", "040000"}

    def replace(token: str) -> str:
        # The scanner alone owns boundaries. Only interpret a terminal citation
        # annotation, and resolve the actual cited file rather than a suffixed name.
        filename, colon, annotation = token.partition(":")
        suffix = colon + annotation
        if suffix and not re.fullmatch(r":[1-9][0-9]*(?::[1-9][0-9]*)?", suffix):
            return token
        path = Path(filename)
        # Do not hide an escape or change the meaning of a symlink/.. walk.
        if ".." in path.parts:
            return token
        for root in roots:
            if not path.is_relative_to(root):
                continue
            try:
                if root is not primary_root and _removed_checkout_root(root, primary_root, reviewed):
                    cited = PurePosixPath(path.relative_to(root).as_posix())
                    return cited.as_posix() + suffix if removed_checkout_citation(cited, suffix) else token
                resolved_root = root.resolve(strict=True)
                resolved_primary = primary_root.resolve(strict=True)
                if (
                    not resolved_primary.is_dir()
                    or not resolved_root.is_dir()
                    or resolved_root == Path(resolved_root.anchor)
                    or not resolved_root.is_relative_to(resolved_primary)
                ):
                    continue
                # Resolve parents to reject escapes, but cite the final directory
                # entry itself: the project interpreter can link outside the repo.
                resolved_path = resolved_root if path == root else path.parent.resolve(strict=True) / path.name
                if not resolved_path.is_relative_to(resolved_root):
                    return token
                mode = resolved_path.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                    return token
                if suffix and (not stat.S_ISREG(mode) or Path(token).exists() or Path(token).is_symlink()):
                    return token
            except (OSError, RuntimeError, ValueError):
                return token
            return path.relative_to(root).as_posix() + suffix
        return token

    # Normalization can compose across a token boundary or reveal traversal.
    # Preserve the entire altered line so its absolute paths reach the scanner.
    lines = []
    for line in reply.splitlines(keepends=True):
        rewritten = line
        spans = absolute_path_spans(line)
        if normalize_for_scan(line) == line:
            for start, end in reversed(spans):
                rewritten = rewritten[:start] + replace(line[start:end]) + rewritten[end:]
            if normalize_for_scan(rewritten) != rewritten:
                rewritten = line
        lines.append(rewritten)
    return "".join(lines)


def _review_branch(task: dict[str, Any]) -> str:
    """Resolve reviewed identity without making it a checkout or cleanup branch."""
    from lu_machinery.common.git_context import UnsafeBranchNameError, validate_plain_branch_name
    from lu_machinery.guardrails.worktree_containment import PROTECTED_BRANCHES

    if any(task.get(key) for key in ("trial_label", "trial_role", "trial_qualified_task")):
        raise RecordError("MODEL_ROLE_TRIAL_REFUSED: a trial never supplies review of record")

    # V2 owns a separate checkout branch; only review_branch identifies its PR.
    fields = ("review_branch",) if task.get("schema_version") == 2 else ("review_branch", "worktree_branch")
    branches = []
    for field in fields:
        value = task.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise RecordError("invalid review branch identity")
        try:
            branch = validate_plain_branch_name(value, repo_root=".")
        except UnsafeBranchNameError as exc:
            raise RecordError("invalid review branch identity") from exc
        if branch in PROTECTED_BRANCHES:
            raise RecordError("protected review branch identity")
        branches.append(branch)
    if not branches:
        raise RecordError("review was not branch-pinned; re-run with --branch <PR head ref>")
    if len(set(branches)) != 1:
        raise RecordError("conflicting review branch identities")
    return branches[0]


def _task(task_id: str, task_root: Path) -> tuple[dict[str, Any], str]:
    """Detect accidental result edits before deriving a publication copy.

    A deliberate same-user writer can also replace the record's stored hash.
    """
    with task_archive_lock(task_root):
        return _task_in_archive_lock(task_id, task_root)


def _task_in_archive_lock(task_id: str, task_root: Path) -> tuple[dict[str, Any], str]:
    """``_task`` for a caller that already holds ``task_archive_lock(task_root)``."""
    if not REVIEW_TASK_ID.fullmatch(task_id) or ".." in task_id:
        raise RecordError("invalid canonical task identity")
    try:
        task_path = _hot_or_archived(task_root, f"{task_id}.json")
        task = json_depth.loads(task_path.read_text(encoding="utf-8"), max_depth=_JSON_MAX_DEPTH)
        raw_reply = task_path.with_suffix(".result").read_bytes()
    except (OSError, ValueError) as exc:
        raise RecordError("review task record or reply unavailable") from exc
    if not isinstance(task, dict):
        raise RecordError("review task record malformed")
    # Legacy records have no embedded id; their canonical basename is still authoritative.
    if task.get("task_id", task_path.stem) != task_id:
        raise RecordError("review task record conflicts with canonical task identity")
    if task.get("status") != "done":
        raise RecordError("review task is not done")
    expected = task.get("result_sha256")
    if expected is not None and hashlib.sha256(raw_reply).hexdigest() != expected:
        raise RecordError(f"terminal_result_digest_mismatch: task {task_id}; writer unknown")
    try:
        # Preserve read_text's universal-newline behaviour after checking the raw bytes.
        reply = raw_reply.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise RecordError("review task record or reply unavailable") from exc
    _review_branch(task)
    return task, reply


def _load_linked_issue_labels(pr: dict[str, Any]) -> None:
    """Fill closing issue labels from the fleet repository named by each issue URL."""
    refs = pr.get("closingIssuesReferences", [])
    if not isinstance(refs, list):
        raise RecordError("LINKED_ISSUE_LABELS_UNAVAILABLE: closing references are malformed")
    if not refs:
        return
    try:
        repos = load_fleet_repos()
    except FleetRepoError:
        raise RecordError("LINKED_ISSUE_LABELS_UNAVAILABLE: fleet repository registry is unreadable") from None
    for ref in refs:
        number = ref.get("number") if isinstance(ref, dict) else None
        if type(number) is not int or number < 1:
            raise RecordError("LINKED_ISSUE_LABELS_UNAVAILABLE: closing reference lacks a valid issue number")
        url = ref.get("url")
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)",
            url if isinstance(url, str) else "",
        )
        if match is None or match[2] != str(number):
            raise RecordError(f"LINKED_ISSUE_LABELS_UNAVAILABLE: closing issue #{number} has a missing or inconsistent URL")
        repository = match[1]
        issue = f"{repository}#{number}"
        try:
            resolve_fleet_repo_by_slug(repository, repos=repos)
        except FleetRepoError:
            raise RecordError(f"LINKED_ISSUE_LABELS_UNAVAILABLE: {issue} is outside the fleet registry") from None
        if isinstance(ref.get("labels"), list):
            continue
        try:
            viewed = _run_json(["gh", "issue", "view", str(number), "--repo", repository, "--json", "labels"])
        except RecordError:
            raise RecordError(f"LINKED_ISSUE_LABELS_UNAVAILABLE: {issue} is unreadable") from None
        labels = viewed.get("labels") if isinstance(viewed, dict) else None
        if not isinstance(labels, list):
            raise RecordError(f"LINKED_ISSUE_LABELS_UNAVAILABLE: {issue} returned malformed labels")
        ref["labels"] = labels


def _pr(repository: str, branch: str, number: int | None) -> dict[str, Any]:
    if number is None:
        matches = _run_json(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--head",
                branch,
                "--limit",
                "2",
                "--json",
                "number,headRefOid,headRefName",
            ]
        )
        if not isinstance(matches, list) or len(matches) != 1 or type(matches[0].get("number")) is not int:
            raise RecordError("branch must identify exactly one open PR; pass --pr N")
        number = matches[0]["number"]
    data = _run_json(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repository,
            "--json",
            "number,headRefOid,headRefName,state,labels,closingIssuesReferences",
        ]
    )
    if not isinstance(data, dict) or data.get("state") != "OPEN":
        raise RecordError("PR is not open")
    _load_linked_issue_labels(data)
    # The view is checked. The return stays Any so this function's existing
    # no-any-return baseline row still matches; that baseline is not in scope.
    checked: Any = data
    return checked


def require_no_outside_rejection(comments: Any, sha: str) -> None:
    """A red-team review cannot supersede recorded outside-family dissent (#10437)."""
    if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
        raise RecordError("RED_TEAM_REFUSED: verdict marker data unavailable")
    for item in comments:
        body = item.get("body") if isinstance(item, Mapping) else None
        if not isinstance(body, str):
            raise RecordError("RED_TEAM_REFUSED: verdict marker data unavailable")
        if MARKER_PREFIX not in body:
            continue
        # Older records may retain the reviewer's REQUEST_CHANGES spelling.
        normalized = body.replace("verdict=REQUEST_CHANGES ", "verdict=CHANGES_REQUESTED ").replace(
            "VERDICT: REQUEST_CHANGES\n", "VERDICT: CHANGES_REQUESTED\n"
        )
        marker = parse_marker(normalized)
        if marker is None:
            raise RecordError("RED_TEAM_REFUSED: verdict marker data unavailable")
        if (
            marker["sha"] == sha
            and marker.get("review_mode", "cross_family") != "red_team"
            and marker["verdict"] in REJECTED
        ):
            raise RecordError("RED_TEAM_REFUSED: outside-family rejection exists for this head")


def require_resolved_branch_authors(facts: BranchReviewFacts) -> None:
    """Refuse a review when any branch commit's author family is unknown (C-2902 F1, F5, F9).

    An unknown author excludes no family and has no model, so no reviewer
    can be proved independent of it, in either review mode. A trailer task id
    without a task record is unknown on every harness. The family is read from
    the commit's ``X-Agent`` trailer (or the task record it names), never from
    the git author: the trailer is author-controlled, so a readable catalog
    trailer is trusted as that family. This refuses unresolvable attribution;
    it does not detect a forged catalog trailer. Untrailered history a red-team
    admission receipt froze keeps its own rules (``unresolved_authors``).
    """
    if facts.unresolved_authors:
        raise RecordError(
            "AUTHOR_UNRESOLVED: branch commits have no resolvable author family "
            f"({', '.join(facts.unresolved_authors)}); no reviewer can be proved outside them"
        )


def require_no_same_head_rejection(comments: Sequence[Mapping[str, Any]], sha: str, login: str, verdict: str) -> None:
    """Refuse an APPROVE the keeper would not land at this head, in every review mode (FR-06).

    The keeper's own reading decides: a same-head approval never clears a
    same-head rejection, and an unknown head (an edited or unattested marker,
    ``integration_sweep.lookup_verdict``) admits no approval, so the recorder
    never posts an approval the keeper would ignore.
    """
    if verdict != "APPROVED":
        return
    current = lookup_verdict(comments, sha, login)
    if current.state in REJECTED:
        raise RecordError(
            "APPROVE_REFUSED: a CHANGES_REQUESTED or BLOCKED verdict is recorded at this head; "
            "only an APPROVE at a new head clears it"
        )
    if current.state == "unknown":
        raise RecordError(
            f"APPROVE_REFUSED: the merge-queue keeper reads this head's verdict as unknown ({current.reason}) "
            "and would not land an APPROVE; escalate the verdict record or review a new head"
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    """A repeated key would silently keep its last value, so it makes the budget policy ambiguous."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise yaml.constructor.ConstructorError(None, None, "duplicate key", node.start_mark)
        return super().construct_mapping(node, deep=deep)


def _review_budget_policy(policy: Path, repos: Mapping[str, FleetRepo]) -> dict[str, int]:
    """Every declared budget in ``policy``, keyed by normalized repository; any defect in the file refuses.

    The whole file is validated, whichever repository is asked about: an integer ``version`` of 1,
    no repeated key at any level, every repository a distinct known owner/name, and every
    ``review_rounds`` an integer from 1 to ``MAX_DECLARED_REVIEW_ROUNDS``.
    """
    try:
        document = yaml.load(policy.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise RecordError("review budget unavailable: the review budget policy is unreadable") from None
    version = document.get("version") if isinstance(document, Mapping) else None
    listed = document.get("repositories") if type(version) is int and version == 1 else None
    if not isinstance(listed, Mapping):
        raise RecordError("review budget unavailable: the review budget policy is malformed")
    budgets: dict[str, int] = {}
    for name, row in listed.items():
        key = _budget_key(name, repos) if isinstance(name, str) else "unknown"
        rounds = row.get("review_rounds") if isinstance(row, Mapping) else None
        if key == "unknown" or key in budgets or type(rounds) is not int or not 1 <= rounds <= MAX_DECLARED_REVIEW_ROUNDS:
            raise RecordError(
                f"review budget unavailable: policy entry {name!r} needs a distinct owner/name and one "
                f"review_rounds integer from 1 to {MAX_DECLARED_REVIEW_ROUNDS}"
            )
        budgets[key] = rounds
    return budgets


def _budget_key(repository: str, repos: Mapping[str, FleetRepo]) -> str:
    """The budget policy key of *repository*: either baked slug of a registered row is that row's one key."""
    try:
        if "/" in repository:
            repository = resolve_fleet_repo_by_slug(repository, repos=repos).github
    except FleetRepoError:
        pass  # an unregistered owner/name keys its own budget
    return normalize_repository(repository)


def repository_review_rounds(
    repository: str, policy: Path | None = None, *, tier: ReviewTier | None = "medium",
) -> int:
    """The repository's override or the default cap for the PR's tier.

    An unknown PR tier uses the smallest cap. A branch without a PR uses the
    standard default. An unreadable or malformed policy file refuses.
    """
    try:
        repos = load_fleet_repos()
    except FleetRepoError:
        raise RecordError("review budget unavailable: the fleet repository registry is unreadable") from None
    budgets = _review_budget_policy(REVIEW_BUDGET_POLICY if policy is None else policy, repos)
    return budgets.get(_budget_key(repository, repos), 2 if tier in {None, "low"} else DEFAULT_REVIEW_ROUNDS)


def _pr_files_and_tier(
    adapter: GitHubAdapter, repository: str, number: int,
) -> tuple[list[dict[str, Any]] | None, ReviewTier | None]:
    """Use one PR file snapshot for the budget and reviewer qualifications."""
    try:
        rows = adapter.pr_file_rows(repository, number)
    except SweepError:
        # The budget uses its smallest default and reviewer qualification stays conservative.
        return None, None
    return rows, classify_pr_files(rows)


def _trusted_markers(
    comments: Sequence[Mapping[str, Any]], login: str, *, reject_malformed: bool = False,
) -> list[tuple[int, dict[str, Any]]]:
    """Read trusted verdict markers. Replay requires every such comment to be readable."""
    markers: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(comments):
        body = item.get("body") if isinstance(item, Mapping) else None
        if not isinstance(body, str) or MARKER_PREFIX not in body:
            continue
        if _author_login(item, rest_only=True) != login or item.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        marker = parse_marker(body)
        if marker is None:
            if reject_malformed:
                raise RecordError("malformed trusted verdict marker")
            continue
        if reject_malformed:
            for field in ("created_at", "updated_at"):
                try:
                    timestamp = integration_sweep._timestamp(item.get(field))
                except OverflowError:
                    timestamp = None
                if timestamp is None:
                    raise RecordError("malformed trusted verdict comment timestamps")
        markers.append((index, marker))
    return markers


def _trusted_verdict_shas(comments: Sequence[Mapping[str, Any]], login: str) -> set[str]:
    """Heads named by verdict markers ``login`` posted with a trusted association."""
    return {marker["sha"] for _, marker in _trusted_markers(comments, login)}


def clean_base_merge_descent(repo_root: Path, *, approved: str, head: str, base_tip: str) -> list[str] | None:
    """The base commits ``head`` merges after ``approved``, when that is all it adds; else None.

    The head must be one two-parent merge directly after ``approved``, with
    its second parent on ``base_tip``. Git must merge the parents without a
    conflict, and the head's tree must be exactly Git's merge result (an empty
    remerge diff). This budget-only proof does not change the stricter merge
    proof used for author attribution. Missing objects, conflicts, any edit in
    the merge and proof errors grant no exemption.
    """
    deadline = time.monotonic() + BRANCH_FACTS_TIMEOUT_S
    try:
        entry = _read_commit_entries(repo_root, [head], deadline=deadline)[0]
        parents = [parent["sha"] for parent in entry["parents"]]
        if len(parents) != 2 or parents[0] != approved:
            return None
        with isolated_object_store(repo_root) as env:
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", parents[1], base_tip],
                env=env, capture_output=True, check=False, timeout=30,
            )
            if ancestry.returncode != 0:
                return None
            # Binary-ours and modify/delete conflicts can have no remerge-diff
            # names when Git's conflicted tree already matches the commit.
            merge = subprocess.run(
                ["git", "merge-tree", "--write-tree", "--name-only", "-z", "--no-messages", *parents],
                env=env, capture_output=True, check=False, timeout=30,
            )
            if merge.returncode != 0:  # 1 is a conflict; anything else is an error
                return None
            fields = merge.stdout.decode("utf-8").split("\0")
            if len(fields) != 2 or fields[1] != "" or not SHA.fullmatch(fields[0]):
                return None
            diff = subprocess.run(
                ["git", "show", "--remerge-diff", "--format=", "--name-only", "-z",
                 "--no-renames", "--no-ext-diff", "--no-textconv", head, "--"],
                env=env, capture_output=True, check=True, timeout=30,
            )
            if set(diff.stdout.decode("utf-8").split("\0")) - {""}:
                return None
    except (RecordError, OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return [parents[1]]


def _clean_merge_descent_prover(
    repo_root: Path, head: str, *, fetch: bool = True
) -> Callable[[str, str], list[str] | None]:
    """:func:`clean_base_merge_descent` bound to the fetched default-branch tip, fetched once on first use.

    A tip that cannot be fetched proves no descent.
    """
    tip: list[str | None] = []

    def prove(approved: str, descendant: str) -> list[str] | None:
        if not tip:
            try:
                tip.append(default_branch_review_base(repo_root, head, fetch=fetch)[0])
            except RecordError:
                tip.append(None)
        base_tip = tip[0]
        if base_tip is None:
            return None
        return clean_base_merge_descent(repo_root, approved=approved, head=descendant, base_tip=base_tip)

    return prove


def _learner_content(paths: Iterable[str]) -> bool:
    """An unreadable product policy cannot waive the learner seats."""
    try:
        return integration_sweep.changes_learner_content(paths)
    except DeployPathsError:
        return True


def head_approval_context(repo_root: Path, head: str, *, repository: str, fetch: bool = True) -> dict[str, Any]:
    """Classify this historical head's own diff, rather than the PR's later file list."""
    _, base = default_branch_review_base(repo_root, head, fetch=fetch)
    raw = _facts_git(
        repo_root, ["diff", "--no-renames", "--name-status", "-z", base, head, "--"],
        deadline=time.monotonic() + BRANCH_FACTS_TIMEOUT_S, code=FACTS_TARGET_UNKNOWN,
    )
    fields = raw.decode("utf-8").split("\0")
    if fields.pop() != "" or len(fields) % 2:
        raise RecordError("historical review file statuses unavailable")
    statuses = {"A": "added", "D": "removed", "M": "modified", "T": "changed"}
    rows = [{"filename": path, "status": statuses.get(status, status)}
            for status, path in zip(fields[::2], fields[1::2], strict=True)]
    paths = pr_file_paths(rows)
    return {
        "second_review": False, "repository": repository,
        "learner_content": _learner_content(paths), "low_tier": classify_pr_files(rows) == "low",
    }


def _fully_approved(
    comments: Sequence[Mapping[str, Any]], head: str, login: str, *, second_review: bool = False,
    repository: str | None = None, learner_content: bool = False, low_tier: bool | None = None,
) -> bool:
    """Whether the keeper reads ``head`` as APPROVED, which needs every seat its profile requires (#12020)."""
    return lookup_verdict(
        comments, head, login, second_review=second_review, repository=repository,
        learner_content=learner_content, low_tier=low_tier,
    ).state == "APPROVED"


def budget_exempt_heads(
    comments: Sequence[Mapping[str, Any]],
    sha: str,
    login: str,
    *,
    prove: Callable[[str, str], list[str] | None],
    unrecorded: Iterable[str] = (),
    approval_context: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """The reviewed heads, and ``sha``, that consume no review round, each with its receipt.

    A head is exempt when it descends from the last fully approved head before
    its first verdict through one clean merge of the base branch (``prove``,
    :func:`clean_base_merge_descent`). A head dispatched for review with no
    recorded verdict (``unrecorded``) is weighed like ``sha``, against the last
    fully approved head on record. The exemption changes accounting only:
    every head still needs its own exact-head verdict, no approval carries
    forward, a recorded rejection stands, and no verdict is dropped. Each
    approved anchor grants at most one exempt head. A further merge needs a
    new exact-head approval first; unreviewed merges cannot chain. Exemptions
    are re-proven from Git on every call, so a head whose proof is lost counts.
    """
    markers = _trusted_markers(comments, login)
    first: dict[str, int] = {}
    first_approval: dict[str, int] = {}
    for index, marker in markers:
        head = marker["sha"]
        first.setdefault(head, index)
        if marker["verdict"] == "APPROVED":
            first_approval.setdefault(head, index)
    order = sorted(first.items(), key=lambda item: item[1])
    order.extend((head, len(comments)) for head in dict.fromkeys([*sorted(unrecorded), sha]) if head not in first)
    exempt: dict[str, dict[str, Any]] = {}
    used_anchors: set[str] = set()
    for head, start in order:
        prefix = comments[:start]
        latest: dict[str, int] = {}
        for index, marker in markers:
            if index < start:
                latest[marker["sha"]] = index
        anchor = None
        for candidate, _ in sorted(latest.items(), key=lambda item: item[1], reverse=True):
            # A clean base merge can only follow an approval visible in this prefix.
            if first_approval.get(candidate, start) >= start:
                continue
            try:
                context = approval_context(candidate) if approval_context else {}
            except RecordError:
                # Unavailable historical facts prove no anchor; they do not prevent a repair round.
                continue
            if _fully_approved(prefix, candidate, login, **context):
                anchor = candidate
                break
        if anchor is None or anchor in used_anchors:
            continue
        merges = prove(anchor, head)
        if merges:
            exempt[head] = {"approved_head": anchor, "base_merges": merges}
            used_anchors.add(anchor)
    return exempt


def require_review_round_within_budget(
    comments: Sequence[Mapping[str, Any]],
    sha: str,
    login: str,
    *,
    budget: int,
    pr: int,
    prove_merge_only: Callable[[str, str], list[str] | None] | None = None,
    repair: Callable[[], dict[str, Any] | None] | None = None,
    repair_heads: frozenset[str] = frozenset(),
    dispatched_heads: Callable[[], frozenset[str]] | None = None,
    exemption: Callable[[], dict[str, Any] | None] | None = None,
    recording_seat: tuple[str, str] | None = None,
    recording_run: tuple[str, str] | None = None,
    approval_context: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refuse a verdict that would open a review round beyond this PR's budget.

    A round is one reviewed head: trusted verdicts on this PR are counted
    across heads, and more verdicts at an already reviewed head (a quorum seat,
    a later chunk of the same review, or a retried publication) open no new round.
    A dispatch reserves a round until its record proves there was no verdict.
    A verdict counts before publication. ``dispatched_heads`` returns the charged
    and reserved heads other than ``sha``. Earlier PRs are not read: no GitHub listing finds them
    reliably (search index lag, the 250-commit PR listing cap). Budget end is
    never approval.

    A head that only merges the base branch cleanly after the last fully
    approved head consumes no round (:func:`budget_exempt_heads`, proven with
    ``prove_merge_only``); it is checked only once the counted heads reach the
    budget. No grant or request adds a round. The lineage's one
    repair-verification round (``scripts/review/repair_round.py``) is outside
    the budget: ``repair`` returns its receipt or refuses, and ``repair_heads``
    names the heads it already admitted, which count as no budgeted round.

    The lineage's one uncharged capability failure (``exemption`` returns it, or None; #12717)
    opens no round: its run is left out of ``dispatched_heads`` and its published verdict, if any,
    is left out here, so its head counts only through another run or verdict at it. A head whose
    only verdict is that run's is therefore no already reviewed head: a replacement there is a
    round like any other. When only the exemption admits the round, the round is its one
    replacement, and at recording (``recording_seat``, the reviewer's family and review profile)
    it must be the exempt run's seat. The exempt run itself (``recording_run``, its task and run
    nonce) charges nothing, so a retried publication of its own verdict is admitted. The verdict
    itself stays as published: a BLOCKED is cleared only by an APPROVE at a new head.
    Returns the result fields that record the exception used, else nothing.
    """
    markers = _trusted_markers(comments, login)
    at_head = {(marker["task"], marker["started"], marker["verdict"]) for _, marker in markers if marker["sha"] == sha}
    # Only a head whose one verdict is one run's BLOCKED can be the exempt head; any other reviewed head opens no round.
    if at_head and (len(at_head) > 1 or next(iter(at_head))[2] != "BLOCKED"):
        return {}
    uncharged = exemption() if exemption is not None else None
    exempt_key = (uncharged["task"], uncharged["started"]) if uncharged is not None else None
    recorded = {marker["sha"] for _, marker in markers if (marker["task"], marker["started"]) != exempt_key}
    if sha in recorded or (uncharged is not None and recording_run == (uncharged["task"], uncharged["run_nonce"])):
        return {}
    heads = recorded | (dispatched_heads() if dispatched_heads is not None else frozenset())
    decide = partial(
        _round_decision, comments, sha, login, budget=budget, pr=pr, prove_merge_only=prove_merge_only,
        repair=repair, repair_heads=repair_heads, approval_context=approval_context,
    )
    receipt = decide(recorded, heads, exempt_key)
    if uncharged is None or uncharged["head"] == sha or uncharged["head"] in heads:
        # The exemption frees no head here: a round at the exempt head charges that head itself.
        return receipt
    try:
        # The same decision with the exempt run charged, through every admission path.
        decide({marker["sha"] for _, marker in markers}, heads | {uncharged["head"]}, None)
    except RecordError:
        # Only the exemption admits this round. A second-review replacement needs its companion
        # reviewer of record too; an ordinary replacement still belongs to its failed seat.
        if (recording_seat is not None and recording_seat != uncharged["seat"]
                and not uncharged.get("second_review")):
            raise RecordError(
                f"CAPABILITY_REPLACEMENT_REFUSED: PR #{pr}'s one replacement for the capability failure of "
                f"{uncharged['task']} must be its seat {uncharged['seat']}, not {recording_seat}"
            ) from None
        return {**receipt, "capability_exemption": dict(uncharged)}
    return receipt


def _round_decision(
    comments: Sequence[Mapping[str, Any]],
    sha: str,
    login: str,
    recorded: set[str],
    heads: set[str] | frozenset[str],
    exempt_key: tuple[str, str] | None,
    *,
    budget: int,
    pr: int,
    prove_merge_only: Callable[[str, str], list[str] | None] | None,
    repair: Callable[[], dict[str, Any] | None] | None,
    repair_heads: frozenset[str],
    approval_context: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """:func:`require_review_round_within_budget`'s decision for one count of the charged heads."""
    if len(heads) < budget:
        return {}
    exempt = (
        budget_exempt_heads(comments, sha, login, prove=prove_merge_only, unrecorded=heads - recorded, approval_context=approval_context)
        if prove_merge_only
        else {}
    )
    if sha in exempt:
        return {"budget_exemption": {"reason": CLEAN_BASE_MERGE, **exempt[sha]}}
    counted = heads - set(exempt) - repair_heads
    if len(counted) < budget:
        return {}
    unrecorded = sorted(counted - recorded)
    if unrecorded:
        listed = ", ".join(head[:12] for head in unrecorded)
        note = f" ({len(unrecorded)} dispatched with no recorded verdict: {listed})"
    else:
        note = ""
    if exempt:
        note += f" (and {len(exempt)} clean base merge head(s), which consume no round)"
    if exempt_key is not None:
        note += f" (the capability failure of {exempt_key[0]} consumes no round; its one replacement is spent)"
    exhausted = (
        f"REVIEW_BUDGET_EXHAUSTED: PR #{pr} already has verdicts at {len(counted)} heads{note} and its budget "
        f"is {budget} rounds; budget end is never approval, escalate instead of recording round {len(counted) + 1}"
    )
    # Once per lineage: only a retry of the round already admitted at this head may run it again.
    if repair is not None and not repair_heads - {sha}:
        try:
            receipt = repair()
        except RecordError as exc:
            raise RecordError(f"{exhausted}; {exc}") from None
        if receipt is not None:
            return {"repair_round": receipt}
    raise RecordError(exhausted)


def _repair_round(**kwargs: Any) -> dict[str, Any] | None:
    # The repair round builds on this module, so it is imported only when the budget is spent.
    from lu_machinery.review.repair_round import admit_repair_round

    return admit_repair_round(**kwargs)


@dataclass(frozen=True)
class _Charging:
    """The rounds a PR's review runs charge, beside the lineage's one uncharged capability failure (#12717)."""

    rounds: dict[str, list[str]]
    exemption: dict[str, Any] | None
    crash_recovery: ProviderCrashRecovery | None = None


def _charging(
    *,
    task_root: Path,
    repository: str,
    number: int,
    branch: str,
    comments: Sequence[Mapping[str, Any]],
    login: str,
    sha: str,
) -> _Charging:
    """Heads other than ``sha`` charged or reserved by review runs on record, with their task ids.

    ``exemption`` is the lineage's one uncharged capability failure (``review_lineage.capability_exemption``)
    with its head and seat, read from the PR's capability-failure events. An unreadable task ledger or
    reply refuses: those heads cannot be counted.
    """
    # The lineage reader builds on this module, so it is imported only when a new head is counted.
    from lu_machinery.review.capability_failure import run_started
    from lu_machinery.review.review_lineage import (
        capability_exemption,
        dispatched_rounds,
        lineage_runs,
        provider_crash_recovery,
    )

    runs = lineage_runs(task_root, repository=repository, number=number, branch=branch)
    key = capability_exemption(runs=runs, comments=comments, login=login)
    exemption = None
    if key is not None:
        run = next(run for run in runs if (run.task_id, run_started(run.record)) == key)
        try:
            seat: tuple[str, str] | None = (reviewer_identity(run.record)[1],
                                            "ukrainian" if is_ukrainian_review(run.record) else "code")
        except RecordError:
            seat = None
        exemption = {
            "task": key[0], "started": key[1], "run_nonce": run.record.get("run_nonce"),
            "head": run.record.get("worktree_base_sha"), "seat": seat, "source": "capability-failure-event",
            # Only a second-review-only model's run is surely a second review; any other
            # model's failed run keeps its seat for the replacement.
            "second_review": is_second_review_only(run.record.get("model")),
        }
    rounds = dispatched_rounds(runs=runs, comments=comments, login=login, exclude_head=sha)
    return _Charging(rounds, exemption, provider_crash_recovery(runs, comments, login))


def _crash_recovery_receipt(
    recovery: ProviderCrashRecovery | None, sha: str, recording_run: tuple[str, str] | None,
) -> dict[str, Any] | None:
    """Use the first subsequent dispatch as the replacement. A failed one cannot restart the allowance."""
    if recovery is None:
        return None
    from lu_machinery.review.capability_failure import run_started

    def evidence(run: ReviewRun) -> dict[str, Any]:
        return {
            "task": run.task_id,
            "run_nonce": run.record.get("run_nonce"),
            "started": run_started(run.record),
            "head": run.record["worktree_base_sha"],
        }

    replacement = recovery.replacement
    if replacement is not None and not recovery.replacement_finished():
        status = replacement.record.get("status")
        if recording_run is None and status in ("spawning", "running"):
            raise RecordError(
                f"PROVIDER_CRASH_RECOVERY_UNRESOLVED: replacement {replacement.task_id} is still {status}"
            )
        return None
    if recording_run is not None and (
        replacement is None
        or (replacement.record["worktree_base_sha"] == sha
            and recording_run != (replacement.task_id, replacement.record.get("run_nonce")))
    ):
        return None
    return {
        "failed": evidence(recovery.failed),
        "replacement": evidence(replacement) if replacement is not None else {"head": sha},
    }


def _repair_receipts(task_root: Path) -> Path:
    """Where the repair rounds' durable receipts live: beside the task ledger, never removed."""
    from lu_machinery.review.repair_round import RECEIPT_DIR

    return task_root.parent / RECEIPT_DIR


def budget_admission(
    *,
    repository: str,
    number: int,
    branch: str,
    sha: str,
    budget: int,
    comments: Sequence[Mapping[str, Any]],
    login: str,
    task_root: Path,
    review_root: Path,
    fetch: bool = True,
    charging: Callable[[], _Charging] | None = None,
    recording_run: tuple[str, str] | None = None,
    recording_seat: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The round decision for a verdict at ``sha``, shared by the recorder and ``budget-check``.

    It writes nothing; ``fetch=False`` also leaves the default-branch ref as it is.
    ``charging`` is the caller's own :func:`_charging` read, when it reports it too.
    ``recording_seat`` is the recorded reviewer's family and review profile, None at dispatch.
    """
    rounds = charging or cache(partial(
        _charging,
        task_root=task_root,
        repository=repository,
        number=number,
        branch=branch,
        comments=comments,
        login=login,
        sha=sha,
    ))
    from lu_machinery.review.repair_round import repair_heads

    recovery = None

    @cache
    def charged_heads() -> frozenset[str]:
        nonlocal recovery
        history = rounds()
        recovery = _crash_recovery_receipt(history.crash_recovery, sha, recording_run)
        return frozenset(history.rounds)

    if recording_run is None:
        # A live recovery blocks a duplicate dispatch even at an already reviewed head.
        charged_heads()
    receipts = _repair_receipts(task_root)
    receipt = require_review_round_within_budget(
        comments,
        sha,
        login,
        budget=budget,
        pr=number,
        prove_merge_only=_clean_merge_descent_prover(review_root, sha, fetch=fetch),
        approval_context=cache(partial(head_approval_context, review_root, repository=repository, fetch=fetch)),
        repair=partial(
            _repair_round,
            repository=repository,
            number=number,
            branch=branch,
            head=sha,
            comments=comments,
            login=login,
            task_root=task_root,
            repo_root=review_root,
            receipt_root=receipts,
            fetch=fetch,
        ),
        repair_heads=repair_heads(receipts, repository=repository, number=number, branch=branch),
        dispatched_heads=charged_heads,
        exemption=lambda: rounds().exemption,
        recording_seat=recording_seat,
        recording_run=recording_run,
    )
    if recovery is not None:
        receipt["provider_crash_recovery"] = recovery
    return receipt


def require_no_outside_review_in_flight(task_root: Path, repository: str, sha: str) -> None:
    """Read the active task ledger before red-team admission or publication."""
    try:
        with task_archive_lock(task_root):
            # The shared iterator suppresses enumeration errors. Read locally so
            # both an unreadable directory and a failed glob refuse this guard.
            list(task_root.iterdir())
            for path in sorted(path for path in task_root.glob("*.json") if path.is_file()):
                try:
                    task = read_listed_task_record(path, tasks_dir=task_root)
                except TaskIdentityMismatch as exc:
                    # An exact copy of the named record adds nothing; any other misnamed file refuses.
                    if is_identical_copy(exc):
                        continue
                    raise RecordError(
                        f"RED_TEAM_REFUSED: task ledger unavailable; {misnamed_record_refusal(path)}"
                    ) from exc
                if task is None:
                    continue
                if any(task.get(key) for key in ("trial_label", "trial_role", "trial_qualified_task")):
                    continue
                prep = task.get("worktree_prep")
                prep_sha = prep.get("base_sha") if isinstance(prep, dict) else None
                prep_matches = (
                    isinstance(prep_sha, str)
                    and SHA.fullmatch(prep_sha.casefold())
                    and prep_sha.casefold() == sha.casefold()
                )
                # Prep rows lack review typing and repository metadata. Fail closed:
                # even non-review dispatches preparing this head briefly block red-team.
                review = (
                    task.get("review")
                    or task.get("pr") is not None
                    or task.get("review_attempt")
                    or task.get("require_review_verdict")
                    or task.get("review_profile")
                    or task.get("review_author_model")
                    or task.get("review_risk")
                    or task.get("type") == "review"
                )
                if (
                    (review or prep_matches)
                    and (
                        (
                            isinstance(task.get("repository"), str)
                            and (
                                normalize_repository(task["repository"]) == normalize_repository(repository)
                                or same_repository(task["repository"], repository)
                            )
                        )
                        or (prep_matches and task.get("repository") is None)
                    )
                    and task.get("status") in {"spawning", "running", "queued"}
                    and task.get("review_mode", "cross_family") != "red_team"
                    and (
                        prep_matches
                        or sha
                        in {
                            task.get(field)
                            for field in (
                                "pinned_head",
                                "worktree_base_sha",
                                "review_head",
                                "review_head_sha",
                                "pr_head_sha",
                                "head_sha",
                            )
                        }
                    )
                ):
                    raise RecordError("RED_TEAM_REFUSED: outside-family review in flight for this head")
    except (OSError, ValueError, TypeError) as exc:
        raise RecordError("RED_TEAM_REFUSED: task ledger unavailable") from exc


def _native_grok_reviewer_identity() -> tuple[str, list[str]]:
    """Resolve the native review holder and its own runtime attestations."""
    catalog = load_model_catalog()
    holder = resolve_routing_reference({"role": "legacy_reviewers", "seat": "xai_reviewer"}, catalog)
    return holder, catalog["models"][holder].get("runtime_model_ids", [])


def _require_formal_reviewer(
    *,
    cursor: bool,
    reported: object,
    model: str,
    family: str,
    native_grok: bool = False,
    review_context: Mapping[str, Any] | None = None,
) -> None:
    """Refuse a verdict from an identity the reviewer resolver never selects (#9488).

    Through Cursor only a pinned formal seat counts, and only when the runtime
    reported its display name (``"Grok 4.7 256K High"``): a bare or other-variant
    slug (``grok-4.7``, ``grok-4.7-high-fast``) attests no variant, and Composer,
    Auto and Cursor-routed Claude are unpinned. Native Grok requires an attested
    runtime model (operator decision 2026-10-05, #9769); Kimi never reviews.
    On every harness the model must also
    hold a catalog review role (#9583), so Fable and retired models never approve.
    """
    if cursor:
        admitted = model in FORMAL_CURSOR_REVIEW_MODELS and reported != model
    else:
        admitted = family not in NATIVE_NON_REVIEWER_FAMILIES or (
            native_grok and model == _native_grok_reviewer_identity()[0]
        )
    if not admitted:
        raise RecordError(f"reviewer model unknown: {model!r} is not a formal reviewer on this harness")
    # #9583: a model the catalog gives no review role never approves, on any harness.
    if refusal := activity_role_refusal(model, REVIEW_ACTIVITY, review_context=review_context):
        raise RecordError(f"reviewer model refused: {refusal}")


def reviewer_identity(task: Mapping[str, Any], *, historical_second_review: bool = False) -> tuple[str, str]:
    """The attested reviewer model and family of a review task record, or a refusal.

    The recorder publishes a verdict only for this identity; the approval
    carry-over applies the same rule to every run in a PR's review lineage.
    """
    if any(task.get(key) for key in ("trial_label", "trial_role", "trial_qualified_task")):
        raise RecordError("MODEL_ROLE_TRIAL_REFUSED: a trial never supplies review of record")
    cursor = task.get("agent") == "cursor"
    native_grok = task.get("agent") in {"grok", "grok-build"}
    reported = task.get("resolved_model") if cursor or native_grok else task.get("model")
    if cursor and task.get("resolved_model_known") is not True:
        raise RecordError("Cursor reviewer model unknown")
    source = task.get("resolved_model_source")
    if cursor and not (isinstance(source, str) and source in RUNTIME_REPORTED_MODEL_SOURCES):
        raise RecordError("Cursor reviewer model unattested: its source is not a runtime report")
    if native_grok and (task.get("resolved_model_known") is not True or source != "grok-model-usage"):
        raise RecordError("native Grok reviewer model unattested: modelUsage runtime report required")
    # Only Cursor's runtime reports display names; record its catalog id.
    model = canonical_cursor_model(reported) if cursor and isinstance(reported, str) else reported
    if native_grok:
        holder, admitted_runtime_ids = _native_grok_reviewer_identity()
        if not isinstance(reported, str) or reported not in admitted_runtime_ids:
            raise RecordError("native Grok reviewer model unknown: runtime model is not admitted")
        model = holder
    if not isinstance(model, str) or not model or re.search(r"\s", model):
        raise RecordError("reviewer model unknown")
    family = resolve_family(model)
    if family in UNRESOLVED_AUTHOR_FAMILIES or family == "unknown":
        raise RecordError("reviewer family unknown")
    # A historical second-review receipt still names its model. That model is not a reviewer of record.
    if not (historical_second_review and is_second_review_only(model)):
        _require_formal_reviewer(
            cursor=cursor,
            reported=reported,
            model=model,
            family=family,
            native_grok=native_grok,
            review_context={
                "review_mode": task.get("review_mode"),
                "review_profile": task.get("review_profile"),
                "author_model": task.get("review_author_model"),
                "transport": "native_codex" if task.get("agent") == "codex" else task.get("agent"),
            },
        )
    return model, family


@dataclass(frozen=True)
class _TaskVerdict:
    repository: str
    branch: str
    sha: str
    model: str
    family: str
    verdict: str
    finding_count: int | Literal["unknown"]
    ukrainian: bool
    sources_count: int | None
    started: str


def _task_verdict(task: dict[str, Any], reply: str) -> _TaskVerdict:
    """The verdict a finished review task's record and sealed reply attest, or a refusal."""
    repository = task.get("repository")
    branch = _review_branch(task)
    sha = task.get("worktree_base_sha")
    if not isinstance(repository, str) or "/" not in repository:
        raise RecordError("review repository unavailable")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise RecordError("reviewed SHA missing or invalid")
    if task.get("pinned_head") is not None and task["pinned_head"] != sha:
        raise RecordError("reviewed SHA conflicts with pinned head")
    model, family = reviewer_identity(task)
    verdict, finding_count = _item_judgments_or_review_verdict(task, reply)
    _require_sources_mcp_calls(task)
    sources_count = task.get("sources_mcp_call_count")
    if type(sources_count) is not int or sources_count < 0:
        sources_count = None
    started_dt = datetime.fromisoformat(str(task.get("started_at") or "").replace("Z", "+00:00"))
    if started_dt.tzinfo is None:
        raise RecordError("review start timestamp missing timezone")
    started = started_dt.astimezone(UTC).isoformat(timespec="microseconds")
    return _TaskVerdict(
        repository,
        branch,
        sha,
        model,
        family,
        verdict,
        finding_count,
        is_ukrainian_review(task),
        sources_count,
        started,
    )


@dataclass(frozen=True)
class _ReviewGitHub:
    """Where a review's repository is read and written on GitHub, and how local records name it."""

    active: str
    stored: str
    selector: dict[str, str] | None


def _review_github(repository: str) -> _ReviewGitHub:
    """Bind the review task's repository to its registry row, read and written at the row's active slug.

    Task records keep the slug they were dispatched with, so either baked slug of a row names it; the
    publication ledger keeps the row's registry slug. An unregistered spelling is kept as stored: the
    checkout lookup (``_review_repository_root``) refuses it. A registered dual-slug row without a
    trusted selector is refused.
    """
    try:
        repos = load_fleet_repos()
        resolve_fleet_repo_by_slug(repository, repos=repos)
    except FleetRepoError:
        return _ReviewGitHub(repository, repository, None)
    try:
        active = resolve_payload_fleet_repo(repository, repos=repos)
    except FleetRepoError as exc:
        raise RecordError(f"review repository unresolved: {exc}") from None
    return _ReviewGitHub(active.active_github, active.repo.github, active.selector_receipt())


def _review_repository_root(repository: str, root: Path) -> Path:
    """The registered primary checkout of the task's target repository (either baked slug of its row)."""
    try:
        key = resolve_payload_fleet_repo(repository, repos=load_fleet_repos()).repo.key
    except FleetRepoError as exc:
        raise RecordError(f"review repository is not uniquely registered: {exc}") from None
    try:
        return resolve_review_repository(root, key, mode="pr")
    except (FleetRepoError, TargetResolutionError, OSError, subprocess.TimeoutExpired):
        raise RecordError("review repository checkout unavailable") from None


def _post_verdict(repository: str, number: int, comment: str, login: str) -> dict[str, Any]:
    """Post one verdict comment and read it back unedited from ``login``; returns the read-back comment."""
    data = _run_json(
        # The typed record is the publisher's only admitted marker carrier (#11127).
        Request("issue-comment-json", repo=repository, number=number, body=CfVerdictRecord(comment)),
    )
    comment_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(comment_id, int):
        raise RecordError("comment post response lacked id; retry to reconcile")
    readback = _run_json(Request("read-comment", repo=repository, number=comment_id))
    if (
        not isinstance(readback, dict)
        or readback.get("body") != comment
        or _author_login(readback, rest_only=True) != login
        or readback.get("author_association") not in TRUSTED_ASSOCIATIONS
        or readback.get("created_at") != readback.get("updated_at")
    ):
        raise RecordError("comment readback mismatch; publication state unknown")
    return readback


def publication_of(
    repository: str, number: int, comment: Mapping[str, Any], *, fleet_repo_selector: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """The publication-ledger entry a posted verdict comment proves, read as the verdict reader reads it (#902).

    Returns (entry, "") or (None, reason). A voided chunk, a second review and a marker of a model that cannot
    supply a review of record are no review of record. The caller decides whether the comment's author is trusted.
    ``fleet_repo_selector`` is the selector receipt the caller resolved the repository with (provenance only).
    """
    from lu_machinery.api import model_stats_rows

    body = comment.get("body")
    marker = parse_marker(body) if isinstance(body, str) else None
    if marker is None:
        return None, "no intact verdict marker"
    if marker.get("void"):
        return None, "voided chunk"
    if is_second_review(marker) or not reviews_of_record(marker["model"], recorded=True):
        return None, "not a reviewer of record"
    verdict = integration_sweep.VERDICT_ALIASES.get(marker["verdict"], marker["verdict"])
    try:
        entry = model_stats_rows.publication(
            task_id=marker["task"], repository=repository, pr=number, head=marker["sha"], verdict=verdict,
            published_at=comment.get("created_at"), fleet_repo_selector=fleet_repo_selector,
        )
    except ValueError:
        return None, "malformed field"
    return entry, ""


def _recovery_command(repository: str, number: int) -> str:
    return f"{RECORDER_COMMAND} import-publications --repository {repository} --pr {number}"


def _store_publication(
    task_root: Path, *, repository: str, number: int, comment: Mapping[str, Any],
    fleet_repo_selector: Mapping[str, Any] | None = None,
) -> str:
    """Store the local marker of a verdict that is now on the PR (#902); the model statistics count only these.

    The marker goes to the publication ledger beside the task store (``model_stats_rows.record_publications``). A
    failure never undoes the publication: the result names it and the one-PR import that stores it later.
    """
    from lu_machinery.api import model_stats_rows

    try:
        entry, reason = publication_of(repository, number, comment, fleet_repo_selector=fleet_repo_selector)
        if entry is None:
            return f"not stored: {reason}"
        model_stats_rows.record_publications(task_root.parent, [entry], wait_s=model_stats_rows.SETTLEMENT_LOCK_WAIT_S)
    except Exception as exc:  # the verdict is already published; name the failure, never raise past it
        return f"failed: {type(exc).__name__}; store it with: {_recovery_command(repository, number)}"
    return "stored"


# A later review contests an author's outcome label with this line in its reply's ``overall.explanation``.
_CONTEST_LINE = re.compile(r"^[ \t]*CONTEST[ \t]+([A-Za-z0-9][A-Za-z0-9._:-]{0,63})[ \t]*:[ \t]*\S", re.MULTILINE)
MAX_EVIDENCE_CHARS = 500
# The findings a CHANGES_REQUESTED review blocks with, and so the ones an author labels: every reachable P0-P2
# finding is an in-scope blocker (review policy section 3); a P3-only reply is published as an approval.
LABELLED_PRIORITIES = frozenset({"P0", "P1", "P2"})


def _structured_reply(reply: str) -> tuple[Mapping[str, Any], Any] | None:
    """The payload and checked findings of a valid ``code-review-findings.v1`` reply, else None."""
    try:
        payload = parse_reviewer_json(reply)
        _, evidence = checked_findings_payload(payload)
    except (ContractError, ReviewPublicationError):
        return None
    return (payload, evidence) if evidence is not None and isinstance(payload, Mapping) else None


def _blocking_finding_ids(reply: str) -> frozenset[str] | None:
    """The ids of the P0-P2 findings of a structured reply; None when the reply has no structured findings."""
    structured = _structured_reply(reply)
    if structured is None:
        return None
    return frozenset(finding.finding_id for finding in structured[1].findings
                     if finding.priority in LABELLED_PRIORITIES)


def _contested_ids(reply: str) -> tuple[str, ...]:
    """The finding ids a structured reply contests: each ``CONTEST <id>: <reason>`` line of its explanation."""
    structured = _structured_reply(reply)
    overall = structured[0].get("overall") if structured is not None else None
    text = overall.get("explanation") if isinstance(overall, Mapping) else None
    return tuple(dict.fromkeys(_CONTEST_LINE.findall(text))) if isinstance(text, str) else ()


def _run_records(task_id: str, task_root: Path) -> int:
    """How many run records the task store keeps under ``task_id``, hot and archived, by dispatch's naming grammar
    (a run dispatched again keeps its earlier record under a stamped name). The caller holds
    ``task_archive_lock(task_root)``, so an archive or restore move cannot shift a record between the two listings.
    RecordError when a directory is unreadable."""
    from lu_machinery.orchestration.task_record_store import candidate_task_record_names

    found = 0
    for directory in (task_root, task_root / ARCHIVE_DIR_NAME):
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RecordError(f"task store unreadable: {type(exc).__name__}") from None
        found += len(candidate_task_record_names(task_id, names))
    return found


def _normalized_started(record: Mapping[str, Any]) -> str | None:
    """``capability_failure.run_started`` with UTC-overflow timestamps returning None instead of raising."""
    from lu_machinery.review.capability_failure import run_started

    try:
        return run_started(record)
    except OverflowError:
        return None


def record_finding_outcome(
    task_id: str, *, pr_number: int, finding_id: str, label: str, evidence: str | None,
    task_root: Path | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Store the author's outcome label for one P0-P2 finding of a review whose CHANGES_REQUESTED verdict the ledger
    holds on PR ``pr_number``. One label per finding: the same label again is a no-op, another one is refused."""
    from lu_machinery.api import model_stats_rows
    from lu_machinery.common.task_store_paths import tasks_dir
    from lu_machinery.review.findings import EVIDENCED_OUTCOME_LABELS, FINDING_OUTCOME_LABELS

    task_root = task_root or tasks_dir()
    if label not in FINDING_OUTCOME_LABELS:
        raise RecordError(f"unknown outcome label {label!r}; use one of: {', '.join(FINDING_OUTCOME_LABELS)}")
    evidence = evidence.strip() if evidence is not None else None
    if label in EVIDENCED_OUTCOME_LABELS and not evidence:
        raise RecordError(f"label {label} needs --evidence: one line that shows it")
    if evidence and (len(evidence) > MAX_EVIDENCE_CHARS or any(ch in evidence for ch in "\n\r\x85  ")):
        raise RecordError(f"evidence must be one line of at most {MAX_EVIDENCE_CHARS} characters")
    # One observation: the reply, the run census and the label's time, under the lock archive and restore moves take.
    with task_archive_lock(task_root):
        task, reply = _task_in_archive_lock(task_id, task_root)
        runs = _run_records(task_id, task_root)
        observed = now or datetime.now(UTC)
    blocking = _blocking_finding_ids(reply)
    if blocking is None:
        raise RecordError("review reply has no structured findings: only code-review-findings.v1 findings take a label")
    if finding_id not in blocking:
        raise RecordError(f"finding {finding_id!r} is not a P0-P2 finding of review {task_id}")
    repository, reviewed = task.get("repository"), task.get("worktree_base_sha")
    if not isinstance(repository, str) or not isinstance(reviewed, str) or not SHA.fullmatch(reviewed):
        raise RecordError(f"review {task_id} names no repository and reviewed head")
    if task.get("pinned_head") is not None and task["pinned_head"] != reviewed:
        raise RecordError("reviewed SHA conflicts with pinned head")
    started = _normalized_started(task)
    started_at = model_stats_rows.timestamp(started) if started is not None else None
    if started is None or started_at is None:
        raise RecordError(f"review {task_id} has no valid start timestamp")
    # A task id can be dispatched again (the older run stays under a stamped name), and the label ledger keys a finding
    # by task id, not by run: label only a review whose id names one run, the one whose reply was read.
    if runs != 1:
        raise RecordError(f"review task id {task_id} names {runs} runs in the task store: a label binds one run")
    # Only the review's own publication: its repository, this PR, the head it reviewed, and posted at or after start.
    started_floor = started_at.replace(microsecond=0)
    try:
        published = [
            entry for entry in model_stats_rows.read_publications(task_root.parent)[0].get(task_id, [])
            if entry["pr"] == pr_number and entry["head"] == reviewed and entry["verdict"] == "CHANGES_REQUESTED"
            and same_repository(entry["repository"], repository)
            and (pub_at := model_stats_rows.timestamp(entry["published_at"])) is not None
            and pub_at >= started_floor
        ]
    except FleetRepoError as exc:
        raise RecordError(f"fleet repository registry unavailable: {exc}") from None
    if not published:
        raise RecordError(f"no published CHANGES_REQUESTED verdict of review {task_id} on PR {pr_number} of "
                          f"{repository} at its reviewed head in the publication ledger; record the verdict first, "
                          f"or import it: {RECORDER_COMMAND} import-publications --repository {repository} "
                          f"--pr {pr_number}")
    attested_model, _ = reviewer_identity(task)
    reviewer_model = model_stats_rows.model_id(attested_model)
    if reviewer_model == "unknown":
        raise RecordError("reviewer model unknown")
    author = os.environ.get("LEARN_UKRAINIAN_DISPATCH_TASK_ID")
    try:
        entry = model_stats_rows.finding_outcome(
            kind="label", task_id=task_id, repository=published[0]["repository"], pr=pr_number,
            finding_id=finding_id, head=reviewed, started_at=started, reviewer_model=reviewer_model, label=label,
            evidence=evidence or None, recorded_by=author if model_stats_rows.valid_task_id(author) else None,
            recorded_at=observed.isoformat())
        added = model_stats_rows.record_finding_outcomes(task_root.parent, [entry])
    except model_stats_rows.OutcomeConflict as exc:
        raise RecordError(f"{exc}: a finding takes one label") from None
    except (FleetRepoError, TimeoutError, OSError) as exc:
        raise RecordError(f"finding-outcome ledger unavailable: {type(exc).__name__}") from None
    return {"task": task_id, "pr": pr_number, "finding": finding_id, "label": label,
            "reviewer_model": reviewer_model, "status": "recorded" if added else "already recorded"}


def _pr_verdict_entries(
    comments: Iterable[Mapping[str, Any]], login: str, *, repository: str, number: int,
    selector: Mapping[str, Any] | None,
) -> tuple[list[tuple[dict[str, Any], str]], int, int]:
    """The publication entries of a PR's verdict markers that ``login`` posted with a permitted association and never
    edited, each beside its marker's run start, in the comments' order (GitHub returns them oldest first), and the
    counts of excluded and skipped markers. ``record`` and ``import-publications`` accept a marker by this one rule.
    The start names the run: a task id can be dispatched again, so the id alone never names one reply."""
    entries: list[tuple[dict[str, Any], str]] = []
    excluded = skipped = 0
    for item in comments:
        body = item.get("body")
        if not isinstance(body, str) or MARKER_PREFIX not in body:
            continue
        association = item.get("author_association")
        created = item.get("created_at")
        if (
            _author_login(item, rest_only=True) != login
            or not isinstance(association, str)
            or association not in TRUSTED_ASSOCIATIONS
            or not isinstance(created, str)
            or created != item.get("updated_at")
        ):
            excluded += 1
            continue
        entry, _ = publication_of(repository, number, item, fleet_repo_selector=selector)
        marker = parse_marker(body)
        if entry is None or marker is None:
            skipped += 1
        else:
            entries.append((entry, marker["started"]))
    return entries, excluded, skipped


def _store_contests(task_root: Path, task_id: str, reply: str, *,
                    publications: Sequence[tuple[Mapping[str, Any], str]], head: str, started: str) -> str | None:
    """Store each ``CONTEST <id>`` line of a published review against the PR's previous blocking review of record.

    ``publications`` is the PR's verdict entries read from its comments, oldest first, each beside its
    run start (``_pr_verdict_entries``): the previous round is the newest earlier head with any verdict (a round
    reviews one head; a publication at this review's own head is a sibling seat of the same round), and it must hold
    exactly one CHANGES_REQUESTED review of another task, the target; otherwise the contest is unresolved. The
    binding never depends on which markers the local ledger holds, and a rerun binds the same round. The target's reply is read only from the run that publication names: a task id can be dispatched again,
    so a task record whose reviewed head or start differs from the marker's is another run, and nothing is stored.
    An id that is no P0-P2 finding of that reply is reported unmatched and never stored; a reply that matches none
    stores nothing. None when the reply contests nothing; never raises."""
    from lu_machinery.api import model_stats_rows

    contested = _contested_ids(reply)
    if not contested:
        return None
    try:
        own = next((index for index, (entry, run) in enumerate(publications)
                    if entry["task_id"] == task_id and entry["head"] == head and run == started), None)
        if own is None:
            return "not stored: no trusted verdict comment of this run on the PR"
        # A round reviews one head, and a new round needs a new head: a publication at this review's own head is a
        # sibling seat of the same round, never the round it contests. The previous round is the newest earlier head
        # with any verdict; a contest binds only to it, never to an older round past it.
        earlier = [(entry, run) for entry, run in reversed(publications[:own]) if entry["head"] != head]
        if not earlier:
            return f"not stored: no earlier round on PR {publications[own][0]['pr']}"
        round_head = earlier[0][0]["head"]
        round_blocking = [(entry, run) for entry, run in earlier
                          if entry["head"] == round_head and entry["verdict"] == "CHANGES_REQUESTED"]
        seats = {entry["task_id"] for entry, _ in round_blocking}
        others = seats - {task_id}
        if len(others) != 1 or task_id in seats:
            return (f"not stored: unresolved: the previous round at {round_head[:12]} has {len(others)} blocking "
                    f"review(s) of another task" + ("; this task id blocked it too" if task_id in seats else ""))
        target, target_started = round_blocking[0]
        record, target_reply = _task(target["task_id"], task_root)
        if record.get("worktree_base_sha") != target["head"] or _normalized_started(record) != target_started:
            return (f"not stored: unresolved: the task record of {target['task_id']} is not the run published at "
                    f"{target['head'][:12]}")
        blocking = _blocking_finding_ids(target_reply) or frozenset()
        matched = [finding_id for finding_id in contested if finding_id in blocking]
        unmatched = [finding_id for finding_id in contested if finding_id not in blocking]
        if not matched:
            return f"not stored: unmatched: {', '.join(unmatched)}"
        moment = datetime.now(UTC).isoformat()
        model_stats_rows.record_finding_outcomes(task_root.parent, [model_stats_rows.finding_outcome(
            kind="contest", task_id=target["task_id"], repository=target["repository"], pr=target["pr"],
            finding_id=finding_id, head=target["head"], started_at=target_started, contested_by=task_id,
            recorded_at=moment) for finding_id in matched],
            wait_s=model_stats_rows.SETTLEMENT_LOCK_WAIT_S)
    except Exception as exc:  # the verdict is already published; name the failure, never raise past it
        return f"failed: {type(exc).__name__}"
    return (f"stored against {target['task_id']}: {', '.join(matched)}"
            + (f"; unmatched: {', '.join(unmatched)}" if unmatched else ""))


def finding_outcome_main(argv: list[str]) -> int:
    from lu_machinery.review.findings import FINDING_OUTCOME_LABELS

    parser = argparse.ArgumentParser(
        prog=f"{RECORDER_COMMAND} finding-outcome",
        description="Record what became of one blocking (P0-P2) finding of a published CHANGES_REQUESTED review.\n"
        "The author records one label per finding before the next round; that round's reviewer may contest it\n"
        "with a line 'CONTEST <finding-id>: <reason>' in its reply's overall.explanation. Labels:\n"
        + "".join(f"  {name}: {meaning}\n" for name, meaning in FINDING_OUTCOME_LABELS.items())
        + "The ledger keeps the label and the evidence's SHA-256, never the evidence text.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Examples:\n  {RECORDER_COMMAND} finding-outcome --task-id review-unit --pr 1 --finding F1 --label fixed\n"
        f"  {RECORDER_COMMAND} finding-outcome --task-id review-unit --pr 1 --finding F2 --label wrong "
        "--evidence 'tests/x.py::test_y covers the None case'\n"
        "Outputs: JSON receipt on stdout, status 'recorded' or 'already recorded'.\n"
        "Exit codes: 0: recorded or already recorded; 1: refused; 2: primary bootstrap refused.",
    )
    parser.add_argument("--task-id", required=True, help="Review task whose blocking verdict is published")
    parser.add_argument("--pr", type=int, required=True, help="PR number the verdict is published on")
    parser.add_argument("--finding", required=True, help="Id of one P0-P2 finding of that review's reply")
    parser.add_argument("--label", required=True, choices=tuple(FINDING_OUTCOME_LABELS), help="The finding's outcome")
    parser.add_argument("--evidence", help="One line backing the label; required for every label but fixed")
    args = parser.parse_args(argv)
    try:
        result = record_finding_outcome(args.task_id, pr_number=args.pr, finding_id=args.finding, label=args.label,
                                        evidence=args.evidence)
    except RecordError as exc:
        print(f"finding outcome refused: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def import_publications(
    *, days: int, dry_run: bool, task_root: Path | None = None, now: datetime | None = None,
    adapter: GitHubAdapter | None = None, only: tuple[str, int] | None = None,
) -> dict[str, int]:
    """One-shot history import (#902): the verdicts published on the PRs of the last ``days`` days of review rows,
    or on the one PR ``only`` names (repository, number), which also stores a marker a failed ``record`` left out.

    Reads each PR's comments once and keeps the verdict markers the current identity posted with a trusted
    association and never edited, as the recorder itself trusts them, read through ``publication_of``. Appends what
    the ledger lacks (``dry_run``: counts only); entries read before a rate limit stops the reads are still stored.
    Never runs inside the Monitor.
    """
    from lu_machinery.api import model_stats_rows
    from lu_machinery.common.github_client import GitHubRateLimited
    from lu_machinery.common.task_store_paths import tasks_dir

    if days < 1:
        raise RecordError("--days must be at least 1")
    batch_state_dir = (task_root or tasks_dir()).parent
    counts: dict[str, int] = dict.fromkeys(
        ("prs", "prs_read", "prs_unreadable", "rate_limited", "markers", "markers_untrusted", "markers_skipped",
         "rows_unregistered_repo"), 0,
    )
    prs: set[tuple[str, int]] = set()
    if only is not None:
        prs.add(only)
    else:
        since = (now or datetime.now(UTC)) - timedelta(days=days)
        slugs = {
            key.casefold(): repo.github
            for key, repo in load_fleet_repos().items()
            if isinstance(key, str) and key.isascii()
        }
        for row in model_stats_rows.read_rows(batch_state_dir)[0]:
            moment = (model_stats_rows.timestamp(row.get("finished_at"))
                      or model_stats_rows.timestamp(row.get("started_at")))
            if row.get("role") != "review" or not isinstance(row.get("pr"), int) or moment is None or moment < since:
                continue
            raw_repo = row.get("repo") or "public"
            slug = slugs.get(raw_repo.casefold()) if isinstance(raw_repo, str) and raw_repo.isascii() else None
            if slug is None:
                counts["rows_unregistered_repo"] += 1
            else:
                prs.add((slug, row["pr"]))
    counts["prs"] = len(prs)
    adapter = adapter or GitHubAdapter(Path.cwd())
    login = adapter.identity()
    entries: list[dict[str, Any]] = []
    registry = load_fleet_repos()
    for repository, number in sorted(prs):
        # Either baked slug names the row: read it at the active slug, store it under the registry slug.
        try:
            target = resolve_payload_fleet_repo(repository, repos=registry)
        except FleetRepoError as exc:
            raise RecordError(f"publication repository unresolved: {exc}") from None
        try:
            comments = adapter.comments(target.active_github, number)
        except SweepError:
            counts["prs_unreadable"] += 1
            continue
        except GitHubRateLimited:
            counts["rate_limited"] = 1
            break
        counts["prs_read"] += 1
        found, excluded, skipped = _pr_verdict_entries(
            comments, login, repository=target.repo.github, number=number, selector=target.selector_receipt())
        counts["markers_untrusted"] += excluded
        counts["markers_skipped"] += skipped
        entries.extend(entry for entry, _ in found)
    counts["markers"] = len(entries)
    if dry_run:
        present = model_stats_rows.publication_keys(batch_state_dir)
        counts["would_add"] = len({model_stats_rows.publication_key(entry) for entry in entries} - present)
    else:
        counts["added"] = model_stats_rows.record_publications(batch_state_dir, entries)
    return counts


def import_publications_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="record_cf_verdict.py import-publications",
        description="Store verdicts already published on PRs in the local publication ledger\n"
        "(<batch_state>/model-stats/publications.jsonl), which the model statistics count (#902): once after\n"
        "the release installs for the history, or for one PR when record reported a marker it could not store.\n"
        "Reads GitHub; run it by hand, never from the Monitor. Idempotent: an entry already present is not added.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{RECORDER_EXAMPLES}  {RECORDER_COMMAND} import-publications --dry-run\n"
        f"  {RECORDER_COMMAND} import-publications --days 30\n"
        f"  {RECORDER_COMMAND} import-publications --repository owner/name --pr 42\n"
        "Outputs: JSON counts on stdout (prs, prs_read, prs_unreadable, rate_limited, markers, markers_untrusted,\n"
        "markers_skipped, rows_unregistered_repo, and added, or would_add with --dry-run).\n"
        "Exit codes: 0: every PR read and its entries stored (or counted); 1: refused, the ledger unwritable,\n"
        "a PR unreadable or a rate limit hit (what was read is stored; rerun is safe).\n"
        "Related: lu_machinery/api/model_stats_rows.py, docs/runbooks/model-stats.md",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Import the PRs of review rows that finished in the last N days (default: 30)",
    )
    parser.add_argument("--repository", help="With --pr: import this one PR only (owner/name); default: none")
    parser.add_argument("--pr", type=int, help="With --repository: the PR number to import; default: none")
    parser.add_argument("--dry-run", action="store_true", help="Count the entries that would be added; write nothing")
    args = parser.parse_args(argv)
    from lu_machinery.api.model_stats_rows import REPOSITORY_SLUG

    if (args.repository is None) != (args.pr is None) or (args.pr is not None and (
            args.pr < 1 or not REPOSITORY_SLUG.fullmatch(args.repository))):
        parser.error("--repository owner/name and --pr N go together, with a PR number of at least 1")
    from lu_machinery.common.github_client import GitHubRateLimited

    try:
        counts = import_publications(
            days=args.days, dry_run=args.dry_run, only=None if args.pr is None else (args.repository, args.pr),
        )
    except (RecordError, SweepError, FleetRepoError, GitHubRateLimited, OSError, ValueError) as exc:
        print(f"publication import refused: {type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 1 if counts["prs_unreadable"] or counts["rate_limited"] else 0


def _item_judgments_or_review_verdict(task: Mapping[str, Any], reply: str) -> tuple[str, int | Literal["unknown"]]:
    """The verdict of a Ukrainian reply that returns only its judgment block, else the reply's verdict.

    With a frozen item denominator the contract asks for the block alone, so a reply
    with no verdict token takes the block's item rollup.
    """
    if not is_ukrainian_review(task) or recognized_verdicts(reply):
        return _review_verdict_and_count(reply)
    try:
        packet = load_frozen_item_packet(task)
        if packet is None:
            return _review_verdict_and_count(reply)
        verdict = item_judgments_verdict(packet, reply)
    except ContractError as exc:
        raise RecordError(str(exc)) from None
    if reason := invalid_findings_block(reply, stated=None):
        raise RecordError(f"reply carries an invalid findings block: {reason}")
    if declares_incomplete_review(reply):
        raise RecordError("inconclusive: reviewer declares an incomplete review")
    if verdict != "BLOCKED" and declares_missing_capabilities(reply):
        raise RecordError(
            f"reply carries {MISSING_CAPABILITIES_FIELD} with verdict {verdict}; only a BLOCKED reply may"
        )
    return verdict, "unknown"


def _item_chunk(
    task: Mapping[str, Any], reply: str, verdict: str, *, source_claims_validated: bool = False,
) -> ItemChunk | None:
    """Coverage declared by the frozen brief, or None when the brief has no item denominator."""
    try:
        packet = load_frozen_item_packet(task)
        if packet is None and not reply_has_item_judgments(reply):
            return None
        if packet is None:
            raise ContractError(
                "ITEM_JUDGMENTS_WITHOUT_DENOMINATOR: item judgments need the brief's frozen item packet"
            )
        return judge_item_reply(
            packet, reply, stated=verdict, ukrainian=is_ukrainian_review(task),
            source_claims_validated=source_claims_validated,
        )
    except ContractError as exc:
        raise RecordError(str(exc)) from None


def _require_ukrainian_claim_support(
    task: Mapping[str, Any], reply: str, task_id: str, task_root: Path,
) -> None:
    """Recheck the sealed reply against its own captured outputs before any publication."""
    from lu_machinery.dispatcher import store

    binding = task.get("review_attempt")
    ledger_path = None
    events = ()
    if binding is not None:
        if task.get("claims_unsupported"):
            raise RecordError("UKRAINIAN_CLAIMS_UNSUPPORTED: " + json.dumps(
                task.get("dropped_claims") or [{"reason": "attempt_claims_unsupported"}], ensure_ascii=False,
            ))
        if not isinstance(binding, dict) or any(
            not isinstance(binding.get(key), str) or not TASK_ID.fullmatch(binding[key])
            for key in ("review_id", "attempt_id")
        ):
            raise RecordError("UKRAINIAN_CLAIMS_UNSUPPORTED: invalid review-attempt binding")
        ledger_path = store.receipts_root() / binding["review_id"] / f"{binding['attempt_id']}.jsonl"
    else:
        path = _hot_or_archived(task_root, f"logs/{task_id}.stdout.log")
        try:
            events = captured_sources_events(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            events = ()
    try:
        _, checked = validate_ukrainian_reply_claims(
            reply, ledger_path=ledger_path, binding=binding, tool_events=events,
        )
    except ContractError as exc:
        raise RecordError(str(exc)) from None
    if checked.claims_unsupported:
        raise RecordError("UKRAINIAN_CLAIMS_UNSUPPORTED: " + json.dumps(
            list(checked.dropped_claims), ensure_ascii=False,
        ))


@publication_boundary(RecordError)
def record(
    task_id: str,
    *,
    pr_number: int | None = None,
    task_root: Path | None = None,
    lock_root: Path | None = None,
    review_mode: str | None = None,
    red_team_prompt: Path | None = None,
    second_review: bool = False,
) -> dict[str, Any]:
    if second_review:
        raise RecordError("second review is not recorded")
    root = _repo_root() if task_root is None or lock_root is None else None
    from lu_machinery.common.task_store_paths import tasks_dir

    task_root = task_root or tasks_dir()
    lock_root = lock_root or task_root.parent / "locks"
    task, reply = _task(task_id, task_root)
    review_mode = review_mode if review_mode is not None else task.get("review_mode", "cross_family")
    if not isinstance(review_mode, str) or review_mode not in {"cross_family", "red_team"}:
        raise RecordError("unsupported review mode")
    if task.get("review_mode", "cross_family") != review_mode:
        if review_mode == "red_team":
            raise RecordError("review mode conflicts with task: red_team requires stored review_mode=red_team")
        raise RecordError("review mode conflicts with task")
    if review_mode == "red_team":
        if is_ukrainian_review(task) or str(task.get("research_track") or "").strip().casefold().startswith("l2-uk"):
            raise RecordError("RED_TEAM_REFUSED: Ukrainian-language reviews stay cross-family")
        if str(task.get("review_profile") or "code").strip().casefold() != "code":
            raise RecordError("RED_TEAM_REFUSED: code review requires review_profile=code")
        try:
            prompt = red_team_prompt.read_text(encoding="utf-8") if red_team_prompt is not None else ""
        except (OSError, UnicodeError) as exc:
            raise RecordError("red-team prompt unavailable") from exc
        if not re.search(r"\bred[-_ ]team\b", prompt, re.IGNORECASE):
            raise RecordError("explicit adversarial red-team prompt required")
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if digest not in (task.get("prompt_sha256"), task.get("effective_prompt_sha256")):
            raise RecordError("red-team prompt digest does not match completed review task")
    elif red_team_prompt is not None:
        raise RecordError("red-team prompt requires review_mode=red_team")
    review = _task_verdict(task, reply)
    repository, branch, sha = review.repository, review.branch, review.sha
    model, family, verdict, finding_count = review.model, review.family, review.verdict, review.finding_count
    ukrainian, sources_count, started = review.ukrainian, review.sources_count, review.started
    if ukrainian and verdict == "APPROVED":
        _require_ukrainian_claim_support(task, reply, task_id, task_root)
    # Local records, locks and lineage use the registry slug; GitHub is read and written at the active slug.
    github = _review_github(repository)
    repository = github.stored
    pr = _pr(github.active, branch, pr_number)
    if pr.get("headRefName") != branch:
        raise RecordError("review branch does not match PR head ref")
    if pr.get("headRefOid") != sha:
        raise RecordError("PR head moved since review; re-run exact-head review")
    number = pr.get("number")
    if not isinstance(number, int) or number < 1:
        raise RecordError("PR number unavailable")
    # Task records and SHA locks stay in the provenance checkout. Git facts
    # and citations belong to the task's registered target repository.
    review_root = _review_repository_root(repository, root or _repo_root())
    unknown_history = (
        admitted_unknown_history(
            repository=repository,
            branch=branch,
            head_sha=sha,
            task_root=task_root,
            repo_root=review_root,
        )
        if review_mode == "red_team"
        else ()
    )
    facts = pr_review_facts(
        repository,
        number,
        head_sha=sha,
        task_root=task_root,
        repo_root=review_root,
        subject_seats=_task_flag_values(task, "review_subject_seats"),
        subject_families=_task_flag_values(task, "review_subject_families"),
        unknown_history=unknown_history,
        github_repository=github.active,
    )
    if facts.unknown_history:
        frozen = task.get("review_branch_facts")
        if not isinstance(frozen, dict) or frozen.get("unknown_history") != list(facts.unknown_history):
            raise RecordError("unknown history conflicts with completed review task")
    if not facts.existing_families:
        raise RecordError("PR has no attributed author commits")
    file_rows, tier = _pr_files_and_tier(GitHubAdapter(review_root), github.active, number)
    low_tier = tier == "low" and not ukrainian and review_mode == "cross_family"
    learner_content = file_rows is None or _learner_content(pr_file_paths(file_rows))
    mixed_pair = (
        review_mode == "cross_family" and not ukrainian and not low_tier and facts.mixed_family
    )
    scope = facts.reviewer_scope(family) if mixed_pair else facts
    if review_mode == "red_team" or mixed_pair or low_tier:
        if low_tier and any(
            commit.family and commit.source != VERIFIED_APP_BOT and not (commit.task_id or commit.record_task_id)
            for commit in facts.commits
        ):
            raise RecordError("AUTHOR_IDENTITY_UNRESOLVED: Low-tier review needs attributed author task identities")
        agent = str(task.get("agent") or "")
        reviewer_ids = {
            key
            for identity in (task_id, task.get("task_id"))
            if isinstance(identity, str)
            for key in review_task_id_comparison_keys(identity, agent=agent)
        }
        author_ids = {key for identity in scope.author_task_ids for key in review_task_id_comparison_keys(identity)}
        if reviewer_ids & author_ids:
            raise RecordError("SELF_REVIEW_DETECTED: reviewer task equals a branch author task")
    if review_mode == "red_team":
        refusal = red_team_seat_refusal(
            (task.get("model"), task.get("resolved_model"), model),
            author_models=facts.author_models,
            author_model=task.get("review_author_model"),
            author_task_models=facts.author_task_models,
        )
        if refusal:
            raise RecordError(refusal)
    # Every mode, red team included (F9): an unresolved author has no model a seat rule can compare.
    require_resolved_branch_authors(facts)
    if review_mode != "red_team" and not low_tier and family in scope.excluded_families:
        raise RecordError("reviewer family equals an author family")
    _require_qualified_reviewer(
        facts, task=task, model=model, family=family, review_mode=review_mode, low_tier=low_tier,
    )
    adapter = GitHubAdapter(Path.cwd())
    login = adapter.identity()
    reply = repository_relative_reply(reply, task=task, primary_root=review_root)
    chunk = _item_chunk(task, reply, verdict, source_claims_validated=ukrainian and verdict == "APPROVED")
    residuals = _p3_finding_ids(reply) if verdict == "APPROVED" else ()
    comment_fields = dict(
        sha=sha,
        task_id=task_id,
        started=started,
        verdict=verdict,
        model=model,
        family=family,
        reply=reply,
        review_mode=review_mode,
        review_profile="ukrainian" if ukrainian else "code",
        sources_mcp_call_count=sources_count,
        unknown_history=facts.unknown_history,
        app_bot=bool(facts.app_bot_commits),
        score=review_score(reply),
        author_models=facts.author_models,
        denominator=None if chunk is None else chunk.denominator,
        coverage=None if chunk is None else chunk.coverage,
        void=None if chunk is None else chunk.void,
        residuals=residuals,
        scope_commits=tuple(commit.sha for commit in scope.commits) if mixed_pair else None,
        second_review=second_review,
    )
    comment = build_comment(**comment_fields)
    destination = normalize_repository(github.active)
    try:
        check_texts(destination, [comment], environment={})
    except PublishBlocked as exc:
        # Only a finding in this body permits a projection. Scanner availability
        # and publication failures retain their existing refusal behavior.
        if exc.indices != frozenset({0}):
            raise
        comment = _withheld_comment(comment_fields, finding_count)
        check_texts(destination, [comment], environment={})
    posted = False
    with pr_lock(repository, number, lock_root):
        # The PR may move while an earlier recorder owns the lock.
        current = _pr(github.active, branch, number)
        if current.get("headRefOid") != sha:
            raise RecordError("PR head moved before publication")
        try:
            comments = adapter.comments(github.active, number)
        except SweepError as exc:
            raise RecordError(f"comment lookup failed: {exc}") from exc
        # The capability failure module builds on this one, so it is imported only here.
        from lu_machinery.review.capability_failure import require_unclassified

        require_unclassified(comments, login, task=task_id, started=started)
        if review_mode == "red_team":
            require_no_outside_rejection(comments, sha)
            require_no_outside_review_in_flight(task_root, repository, sha)
        budget_receipt = budget_admission(
            repository=repository,
            number=number,
            branch=branch,
            sha=sha,
            budget=repository_review_rounds(repository, tier=tier),
            comments=comments,
            login=login,
            task_root=task_root,
            review_root=review_root,
            recording_run=(task_id, task["run_nonce"]) if isinstance(task.get("run_nonce"), str) else None,
            recording_seat=(family, "ukrainian" if ukrainian else "code"),
        )
        from lu_machinery.review.repair_round import receipt_at, record_admission, require_repair_verified

        repair = budget_receipt.get("repair_round")
        repaired = repair or receipt_at(
            _repair_receipts(task_root), repository=repository, number=number, branch=branch, head=sha
        )
        existing = None
        for item in comments:
            body = item.get("body")
            if not isinstance(body, str):
                raise RecordError("comment lookup partial")
            if MARKER_PREFIX in body:
                marker = parse_marker(body)
                if marker is None and f"sha={sha}" in body:
                    raise RecordError("unparseable marker on reviewed SHA")
                if (
                    marker
                    and marker["sha"] == sha
                    and marker["task"] == task_id
                    and _author_login(item, rest_only=True) == login
                    and item.get("author_association") in TRUSTED_ASSOCIATIONS
                ):
                    if (
                        marker["started"] != started
                        or marker["model"] != model
                        or marker["family"] != family
                        or marker.get("review_mode", "cross_family") != review_mode
                        or is_second_review(marker) != second_review
                        or marker.get("unknown_history", []) != list(facts.unknown_history)
                        or marker.get("authorship") != (VERIFIED_APP_BOT if facts.app_bot_commits else None)
                        or marker.get("review_profile", "code") != ("ukrainian" if ukrainian else "code")
                        or marker.get("sources_calls") != sources_count
                        or list(marker.get("denominator") or ()) != list(chunk.denominator if chunk else ())
                        or list(marker.get("coverage") or ()) != list(chunk.coverage if chunk else ())
                        or marker.get("void") != (chunk.void if chunk else None)
                        or tuple(marker.get("scope_commits") or ()) != tuple(sorted(set(comment_fields["scope_commits"] or ())))
                        or item.get("created_at") != item.get("updated_at")
                    ):
                        raise RecordError("existing verdict marker was edited or conflicts with task")
                    if marker["verdict"] == verdict:
                        existing = item
                        break
                    raise RecordError("existing verdict marker was edited or conflicts with task")
        require_no_same_head_rejection(comments, sha, login, verdict)
        if verdict == "APPROVED":
            # Read under the lock, so a security finding another review just posted still binds.
            _require_security_approval(
                reply,
                critical=_critical_review(task, facts),
                critical_seat=_critical_review_seat(task, model),
                inherited=_inherited_security_reviews(
                    comments, login, task_root=task_root, task_id=task_id, started=started,
                    repository=repository, number=number, branch=branch,
                ),
            )
        if repaired is not None and verdict == "APPROVED":
            require_repair_verified(reply, repaired)
        if repair is not None:
            # The admission receipt is durable before the verdict exists, so the round is spent even if posting fails.
            record_admission(_repair_receipts(task_root), repair)
        if existing is None:
            # Verdict, round and receipt are already decided. A security-sensitive
            # review posts the withheld form so the public comment keeps the count only.
            if _security_sensitive(task=task, reply=reply, pr=current):
                comment = _withheld_comment(comment_fields, finding_count)
                check_texts(destination, [comment], environment={})
            posted_comment = _post_verdict(github.active, number, comment, login)
            posted = True
            comments = [*comments, posted_comment]
        if repair is not None:
            from lu_machinery.review.repair_round import record_consumption

            record_consumption(
                _repair_receipts(task_root), repair, comment=str(posted_comment["id"] if posted else existing.get("id"))
            )
        state = "failure"
        pending_reason = ""
        if verdict == "APPROVED" or (chunk is not None and chunk.void):
            # Success is the keeper's own reading of the head with this record, never this
            # approval alone: another Ukrainian seat may still be missing (#12020), and a
            # declared item denominator may still be short (#12257). A voided chunk does not
            # count, so its own verdict does not decide the status.
            current = lookup_verdict(
                comments, sha, login, low_tier=low_tier, learner_content=learner_content,
            )
            state = "success" if current.state == "APPROVED" else "failure" if current.state in REJECTED else "pending"
            if current.state == NEEDS_SEAT and mixed_pair:
                pending_reason = "; awaiting mixed-family review pair"
            elif current.state == NEEDS_SEAT:
                profiled = [
                    marker
                    for item in comments
                    if isinstance(item.get("body"), str)
                    and (marker := parse_marker(item["body"])) is not None
                    and marker.get("sha") == sha
                    and marker.get("verdict") not in REJECTED
                    and is_ukrainian_review(marker)
                ]
                open_seats = sorted(
                    required_ukrainian_seats(attested_author_families(profiled))
                    - set(current.ukrainian_seats)
                    - set(facts.excluded_families)
                )
                pending_reason = f"; awaiting Ukrainian seat: {'/'.join(open_seats) or 'none eligible'}"
            elif state == "pending":
                pending_reason = f"; head reads {current.state}"
        description = (
            f"review_mode=red_team unknown_history={len(facts.unknown_history)} "
            if facts.unknown_history
            else ("review_mode=red_team " if review_mode == "red_team" else "")
        ) + (
            f"VERDICT: {verdict} ({family}, sources={sources_count}) {task_id}"
            if ukrainian
            else f"VERDICT: {verdict} {family} {task_id}"
        )
        # The task id can be long; the pending reason must survive the 140-character limit.
        description = description[: 140 - len(pending_reason)] + pending_reason
        try:
            post_commit_status(
                repository=github.active,
                head_sha=sha,
                state=state,
                context=DEFAULT_STATUS_CONTEXT,
                description=description,
            )
            status = "posted"
        except Exception as exc:
            status = f"failed: {exc}"
        # After the status, so a marker problem never stands between the verdict and its status.
        publication_marker = _store_publication(
            task_root, repository=github.stored, number=number, comment=posted_comment if posted else existing or {},
            fleet_repo_selector=github.selector,
        )
        # The PR's own verdict comments name the round a contest binds, whatever the local ledger holds.
        contests = _store_contests(task_root, task_id, reply, head=sha, started=started,
                                   publications=_pr_verdict_entries(comments, login, repository=github.stored,
                                                                    number=number, selector=github.selector)[0])
    result = {
        "pr": number,
        "head": sha,
        "task": task_id,
        "verdict": verdict,
        "score": comment_fields["score"],
        "review_mode": review_mode,
        "unknown_history": list(facts.unknown_history),
        "comment": "posted" if posted else "existing",
        "status": status,
        "publication_marker": publication_marker,
    }
    if residuals:
        result["residuals"] = list(residuals)
    if contests is not None:
        result["outcome_contests"] = contests
    if github.selector is not None:
        result["fleet_repo_selector"] = github.selector
    if facts.app_bot_commits:
        result.update({"authorship": VERIFIED_APP_BOT, "app_bot_commits": list(facts.app_bot_commits)})
    if chunk is not None:
        result["denominator"] = list(chunk.denominator)
        result["coverage"] = list(chunk.coverage)
        if chunk.void:
            result["void"] = chunk.void
    result.update(budget_receipt)
    return result


def budget_check(
    repository: str, number: int, *, task_root: Path | None = None, head: str | None = None
) -> dict[str, Any]:
    """The recorder's round decision for a verdict at the PR's current head, before any review runs.

    ``unrecorded_rounds`` names charged or reserved heads without a published verdict.
    ``head``, when given, must be the PR's current head.
    """
    root = _repo_root()
    from lu_machinery.common.task_store_paths import tasks_dir

    task_root = task_root or tasks_dir()
    # Local records and lineage use the registry slug; GitHub is read at the active slug.
    github = _review_github(repository)
    repository = github.stored
    pr = _run_json(["gh", "pr", "view", str(number), "--repo", github.active, "--json", "headRefOid,headRefName,state"])
    if not isinstance(pr, dict) or pr.get("state") != "OPEN":
        raise RecordError(f"PR #{number} is not open")
    sha, branch = pr.get("headRefOid"), pr.get("headRefName")
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or not isinstance(branch, str) or not branch:
        raise RecordError("PR head unavailable")
    if head is not None and head != sha:
        raise RecordError(f"PR #{number} head is {sha[:12]}, not the review head {head[:12]}")
    review_root = _review_repository_root(repository, root)
    adapter = GitHubAdapter(review_root)
    _, tier = _pr_files_and_tier(adapter, github.active, number)
    # Verify the head again after reading the PR files. A stale or missing default ref refuses.
    pr_review_facts(repository, number, head_sha=sha, task_root=task_root, repo_root=review_root, fetch=False,
                    github_repository=github.active)
    budget = repository_review_rounds(repository, tier=tier)
    login = adapter.identity()
    try:
        comments = adapter.comments(github.active, number)
    except SweepError as exc:
        raise RecordError(f"comment lookup failed: {exc}") from exc
    charging = cache(
        partial(
            _charging,
            task_root=task_root,
            repository=repository,
            number=number,
            branch=branch,
            comments=comments,
            login=login,
            sha=sha,
        )
    )
    receipt = budget_admission(
        repository=repository,
        number=number,
        branch=branch,
        sha=sha,
        budget=budget,
        comments=comments,
        login=login,
        task_root=task_root,
        review_root=review_root,
        fetch=False,
        charging=charging,
    )
    uncharged = charging().exemption
    exempt_key = (uncharged["task"], uncharged["started"]) if uncharged is not None else None
    # The uncharged capability failure's own verdict reviews no head (#12717).
    heads = {marker["sha"] for _, marker in _trusted_markers(comments, login)
             if (marker["task"], marker["started"]) != exempt_key}
    already = sha in heads
    unrecorded = {head: tasks for head, tasks in charging().rounds.items() if head not in heads}
    result = {
        "pr": number,
        "head": sha,
        "admitted": True,
        "reviewed_heads": len(heads | set(unrecorded)),
        "unrecorded_rounds": [{"head": head, "tasks": tasks} for head, tasks in sorted(unrecorded.items())],
        "budget": budget,
        "head_already_reviewed": already,
        **receipt,
    }
    if uncharged is not None:
        result["uncharged_capability_failure"] = uncharged
    return result


def _open_pr_number(repository: str, branch: str) -> int | None:
    """The number of the one open PR whose head is ``branch``, None when it has none."""
    matches = _run_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--head",
            branch,
            "--limit",
            "2",
            "--json",
            "number",
        ]
    )
    if not isinstance(matches, list) or len(matches) > 1:
        raise RecordError("branch must identify at most one open PR; pass --pr N")
    return matches[0]["number"] if matches else None


def dispatch_budget_check(
    repository: str, *, number: int | None, branch: str | None, head: str, task_root: Path
) -> dict[str, Any] | None:
    """Refuse a review dispatch at ``head`` once its PR's charged or reserved heads reach the budget.

    The dispatcher's own task records name each run's PR or review branch and its head
    (``review_lineage.dispatched_rounds``). A dispatch that names only one of PR and branch has
    the other read from GitHub first, so runs recorded under either count; with no review run of
    the repository on record, no round has been spent and nothing more is read. A head that
    already charged or reserved opens no round. When those heads reach the smallest applicable
    cap, defer to :func:`budget_check` for the PR's actual tier and round exemptions.
    A branch with no open PR uses the repository's standard budget.
    """
    from lu_machinery.review.review_lineage import (
        capability_candidates_only,
        dispatched_rounds,
        lineage_runs,
        provider_crash_recovery,
        review_records,
    )

    with task_archive_lock(task_root):
        records = review_records(task_root, repository=repository)
        if not records:
            # No review of the repository on record, so no round is spent: round 1 fits every budget the
            # policy can declare (1 at least) and nothing here depends on it. The verdict still reads it first.
            return None
        if number is None and branch is not None:
            number = _open_pr_number(_review_github(repository).active, branch)
        elif number is not None and branch is None:
            branch = _pr(_review_github(repository).active, "", number)["headRefName"]
        runs = lineage_runs(task_root, repository=repository, number=number, branch=branch, records=records)
    # The full check must see a recovery even when the head is unchanged or the normal budget has room.
    if number is not None and provider_crash_recovery(runs, (), "") is not None:
        return budget_check(repository, number, task_root=task_root, head=head)
    budget = repository_review_rounds(repository, tier=None if number is not None else "medium")
    heads = dispatched_rounds(runs=runs, comments=(), login="")
    if len(heads) < budget:
        return None
    # A head already counted opens no round, unless only zero-finding BLOCKEDs count there: those may be
    # the lineage's uncharged capability failure, which only the PR's comments and rulings decide (#12717).
    if head in heads and (number is None or not capability_candidates_only(runs, head)):
        return None
    if number is None:
        raise RecordError(
            f"REVIEW_BUDGET_EXHAUSTED: branch {branch} has {len(heads)} charged or reserved heads and no open PR; "
            f"its budget is {budget} rounds; budget end is never approval, escalate instead"
        )
    return budget_check(repository, number, task_root=task_root, head=head)


RECORDER_COMMAND = '"$PRIMARY_REPO/.venv/bin/python" -m lu_machinery.review.record_cf_verdict'
RECORDER_EXAMPLES = (
    'Examples:\n  PRIMARY_REPO="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"\n'
)


def budget_check_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="record_cf_verdict.py budget-check",
        description="Ask the verdict recorder whether a review round at the PR's current head would be admitted.\n"
        "Run it before dispatching a reviewer; it reads only and writes no receipt.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{RECORDER_EXAMPLES}  {RECORDER_COMMAND} budget-check "
        "--repository learn-ukrainian/learn-ukrainian.github.io --pr 42\n"
        "Outputs: JSON on stdout: admitted, the budget, unrecorded_rounds (counted heads without a published verdict)\n"
        "  and any exemption or repair-round receipt.\n"
        "Exit codes: 0: admitted; 1: refused (the reason names what to escalate); 2: primary bootstrap refused.\n"
        "Related: agents_extensions/shared/skills/review-loop/references/policy.md",
    )
    parser.add_argument("--repository", required=True, help="GitHub owner/name of the PR")
    parser.add_argument("--pr", type=int, required=True, help="Open PR number")
    args = parser.parse_args(argv)
    try:
        result = budget_check(args.repository, args.pr)
    except (RecordError, SweepError, ValueError) as exc:
        print(json.dumps({"pr": args.pr, "admitted": False, "refusal": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


@publication_boundary(RecordError)
def republish_withheld(
    task_id: str, *, pr_number: int, task_root: Path | None = None, lock_root: Path | None = None
) -> dict[str, Any]:
    """Publish the findings of a verdict that was recorded withheld, from the task's sealed reply.

    Nothing is recorded again: the reviewed head may have moved, no round,
    receipt or commit status is written, and the withheld comment stays as it
    is (a verdict comment is never edited). The new comment is exactly what the
    recorder publishes for this task now, with the branch facts re-derived at
    the reviewed head, which the PR must still contain. It is refused unless
    the reply's digest is sealed, every recorded verdict of this task is that
    comment or its withheld form, the withheld one is the PR's latest verdict,
    and the reply passes the publication scan.
    """
    root = _repo_root() if task_root is None or lock_root is None else None
    from lu_machinery.common.task_store_paths import tasks_dir

    task_root = task_root or tasks_dir()
    lock_root = lock_root or task_root.parent / "locks"
    task, reply = _task(task_id, task_root)
    if not isinstance(task.get("result_sha256"), str):
        raise RecordError("republication requires the review task's sealed reply digest")
    review = _task_verdict(task, reply)
    review_mode = task.get("review_mode", "cross_family")
    # Local records, locks and lineage use the registry slug; GitHub is read and written at the active slug.
    github = _review_github(review.repository)
    repository = github.stored
    pr = _pr(github.active, review.branch, pr_number)
    if pr.get("headRefName") != review.branch:
        raise RecordError("review branch does not match PR head ref")
    review_root = _review_repository_root(repository, root or _repo_root())
    facts = pr_review_facts(
        repository,
        pr_number,
        head_sha=review.sha,
        task_root=task_root,
        repo_root=review_root,
        subject_seats=_task_flag_values(task, "review_subject_seats"),
        subject_families=_task_flag_values(task, "review_subject_families"),
        unknown_history=(
            admitted_unknown_history(
                repository=repository,
                branch=review.branch,
                head_sha=review.sha,
                task_root=task_root,
                repo_root=review_root,
            )
            if review_mode == "red_team"
            else ()
        ),
        current_head=False,
        github_repository=github.active,
    )
    reply = repository_relative_reply(reply, task=task, primary_root=review_root)

    def rendered(second: bool, verdict: str, scoped: bool) -> tuple[str, list[str]]:
        """Known forms of this recorded comment, and the full form to publish.

        ``scoped`` is the recorded marker's own form: a Low-tier record is unscoped even on a pair branch.
        """
        mixed_pair = (
            scoped and review_mode == "cross_family" and not review.ukrainian and not second and facts.mixed_family
        )
        scope = facts.reviewer_scope(review.family) if mixed_pair else facts
        chunk = _item_chunk(
            task, reply, review.verdict, source_claims_validated=review.ukrainian and review.verdict == "APPROVED",
        )
        residuals = _p3_finding_ids(reply) if verdict == "APPROVED" else ()
        if verdict != review.verdict and not residuals:
            raise RecordError("legacy approval has no sealed P3 findings")
        comment_fields = dict(
            sha=review.sha,
            task_id=task_id,
            started=review.started,
            verdict=verdict,
            model=review.model,
            family=review.family,
            reply=reply,
            review_mode=review_mode,
            review_profile="ukrainian" if review.ukrainian else "code",
            sources_mcp_call_count=review.sources_count,
            unknown_history=facts.unknown_history,
            app_bot=bool(facts.app_bot_commits),
            score=review_score(reply),
            author_models=facts.author_models,
            denominator=None if chunk is None else chunk.denominator,
            coverage=None if chunk is None else chunk.coverage,
            void=None if chunk is None else chunk.void,
            residuals=residuals,
            scope_commits=tuple(commit.sha for commit in scope.commits) if mixed_pair else None,
            second_review=second,
        )
        comment = build_comment(**comment_fields)
        withheld = _withheld_comment(comment_fields, review.finding_count)
        full_forms = [comment]
        if verdict == review.verdict:
            if residuals:
                full_forms.append(build_comment(**{**comment_fields, "residuals": ()}))
            elif verdict != "APPROVED":
                full_forms.append(build_comment(**comment_fields, repair_withheld=True))
        return withheld, full_forms

    adapter = GitHubAdapter(Path.cwd())
    login = adapter.identity()
    result = {"pr": pr_number, "head": review.sha, "task": task_id, "verdict": review.verdict}
    if github.selector is not None:
        result["fleet_repo_selector"] = github.selector
    # The PR lock keeps a verdict at another head from landing between the order check and the post.
    with pr_lock(repository, pr_number, lock_root):
        try:
            comments = adapter.comments(github.active, pr_number)
        except SweepError as exc:
            raise RecordError(f"comment lookup failed: {exc}") from exc
        markers = _trusted_markers(comments, login, reject_malformed=True)
        own_markers = [
            (index, marker) for index, marker in markers if (marker["sha"], marker["task"]) == (review.sha, task_id)
        ]
        if not own_markers:
            raise RecordError("no verdict of this task is recorded at its head")
        own = [comments[index] for index, _ in own_markers]
        if any(item.get("created_at") != item.get("updated_at") for item in own):
            raise RecordError("existing verdict marker was edited")
        _, marker = own_markers[-1]
        verdict = marker["verdict"]
        # A previously published P3 approval already has an APPROVED marker. Match the
        # trusted comment byte for byte before restoring its sealed reply.
        if verdict != review.verdict and (verdict, review.verdict) != ("APPROVED", "CHANGES_REQUESTED"):
            raise RecordError("recorded verdict conflicts with the sealed reply")
        second = is_second_review(marker)
        if (second and not is_second_reviewer_model(review.model)) or (
            not second and not reviews_of_record(review.model)
        ):
            raise RecordError("a recorded verdict of this task does not match its record and sealed reply")
        withheld, full_forms = rendered(second, verdict, scoped=bool(marker.get("review_pair")))
        if any(item["body"] != withheld and item["body"] not in full_forms for item in own):
            raise RecordError("a recorded verdict of this task does not match its record and sealed reply")
        comment = full_forms[0]
        check_texts(normalize_repository(github.active), [comment], environment={})
        if any(item["body"] in full_forms for item in own):
            return {**result, "comment": "existing"}
        if comments[markers[-1][0]] is not own[-1]:
            raise RecordError("a later verdict is recorded on this PR; republishing would reorder the verdicts")
        comment_id = _post_verdict(github.active, pr_number, comment, login)["id"]
    return {**result, "comment": "posted", "comment_id": comment_id}


def republish_withheld_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="record_cf_verdict.py republish-withheld",
        description="Publish the findings of a verdict recorded as withheld, once the cause is fixed.\n"
        "Posts the sealed reply beside the withheld comment; records no new verdict, round or status.\n"
        "This includes a security-sensitive verdict, whose findings stay withheld until this command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{RECORDER_EXAMPLES}  {RECORDER_COMMAND} republish-withheld "
        "--task-id review-unit --pr 42\n"
        "Outputs: JSON on stdout: the PR, head, task, verdict and whether a comment was posted.\n"
        "Exit codes: 0: posted, or the findings were already published; 1: refused; 2: primary bootstrap refused.\n"
        "Related: agents_extensions/shared/skills/review-loop/SKILL.md",
    )
    parser.add_argument("--task-id", required=True, help="Review task whose verdict was recorded withheld")
    parser.add_argument("--pr", type=int, required=True, help="Open PR number the verdict is recorded on")
    args = parser.parse_args(argv)
    try:
        result = republish_withheld(args.task_id, pr_number=args.pr)
    except (RecordError, SweepError, ValueError) as exc:
        print(f"CF verdict republication refused: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


@publication_cli(RecordError)
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["budget-check"]:
        return budget_check_main(argv[1:])
    if argv[:1] == ["republish-withheld"]:
        return republish_withheld_main(argv[1:])
    if argv[:1] == ["import-publications"]:
        return import_publications_main(argv[1:])
    if argv[:1] == ["finding-outcome"]:
        return finding_outcome_main(argv[1:])
    if argv[:1] == ["capability-failure"]:
        # The capability failure verb builds on this module, so it is imported only when chosen.
        from lu_machinery.review.capability_failure import main as capability_failure_main

        return capability_failure_main(argv[1:])
    parser = argparse.ArgumentParser(
        description="Publish a completed exact-head verdict; cross-family is the default.\n"
        "Use after the reviewer exits; same-family needs an explicit prompt-bound red-team review.\n"
        "A security-sensitive review (profile security, a SECURITY_VERDICT or a security finding in the\n"
        "reply, or a security label on the PR or a linked issue) is published with its finding count only.\n"
        "A security finding at any priority never approves. APPROVE on a critical review, or after another\n"
        "review of the PR raised a security finding, needs a critical_review seat and a reply that is one JSON\n"
        f"object whose overall.explanation opens with the line '{SECURITY_ATTESTATION}'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{RECORDER_EXAMPLES}  {RECORDER_COMMAND} --task-id review-unit --pr 1\n"
        f"  {RECORDER_COMMAND} --task-id review-unit --pr 1 "
        "--review-mode red_team --red-team-prompt brief.md\n"
        f"  {RECORDER_COMMAND} budget-check --repository owner/name --pr 1\n"
        "    (whether the next review round is admitted, before dispatch; see budget-check --help)\n"
        f"  {RECORDER_COMMAND} republish-withheld --task-id review-unit --pr 1\n"
        "    (publish findings held back at record time, including a security-sensitive verdict;\n"
        "    see republish-withheld --help)\n"
        f"  {RECORDER_COMMAND} import-publications --dry-run\n"
        "    (one-shot: store the verdicts already on PRs in the local publication ledger;\n"
        "    see import-publications --help)\n"
        f"  {RECORDER_COMMAND} capability-failure --task-id review-unit --pr 1 "
        "--capability test-execution --authority claude-cto\n"
        "    (classify a finished run that only lacked a capability; see capability-failure --help)\n"
        f"  {RECORDER_COMMAND} finding-outcome --task-id review-unit --pr 1 --finding F1 --label fixed\n"
        "    (label one blocking finding of a published review; see finding-outcome --help)\n"
        "Outputs: GitHub review comment and commit status; JSON publication receipt on stdout.\n"
        "Exit codes: 0: recorded; 1: refused or status publication failed; 2: primary bootstrap refused.\nRelated: #9297, #9951",
    )
    parser.add_argument("--task-id", required=True, help="Completed branch-pinned review task id")
    parser.add_argument("--pr", type=int, help="Open PR number; otherwise resolve one open PR from the branch")
    parser.add_argument(
        "--review-mode",
        choices=("cross_family", "red_team"),
        help="Review mode (default: task review_mode, otherwise cross_family); red_team permits same-family",
    )
    parser.add_argument(
        "--red-team-prompt",
        type=Path,
        help="Prompt file used by the completed red-team task; required for red_team, default: none",
    )
    args = parser.parse_args(argv)
    try:
        result = record(
            args.task_id, pr_number=args.pr, review_mode=args.review_mode, red_team_prompt=args.red_team_prompt,
        )
    except (RecordError, SweepError, ValueError) as exc:
        print(f"CF verdict refused: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "posted" else 1


if __name__ == "__main__":
    # Run as a file or with ``-m``, this module is ``__main__`` and defines its own ``RecordError``; the
    # refusals other modules raise subclass the package module's, so ``main`` must come from that one.
    from lu_machinery.review.record_cf_verdict import main as package_main

    raise SystemExit(package_main())

"""Durable per-task summary rows behind the model statistics (#581), and their one-time backfill.

One JSON line per settled dispatch task in ``batch_state/model-stats/rows.jsonl``: the concrete model, the
effective effort, role, class, terminal status, timings, normalised tokens, allowance readings, the review
verdict and finding priorities, and the landing reference the task recorded. The file lives beside the task
store, not in it, so task-record archiving and retention never touch it, and the statistics can cover any
window back to the first record.

Writers: the dispatcher appends a row each time it writes a settled record (``record_settled``; the last row
of a task id wins, so the final update decides), and ``backfill`` adds a row for every settled record that has
none. Both hold ``rows.lock``; neither ever changes a task record. Rows carry labels, counts, timestamps, PR
numbers and commit SHAs only, plus, for a structured review, a 16-hex digest of each blocking finding's file path
(``blocking_paths``, never the path), and for an unparsed review reply, its typed ``unparsed_reason`` and up to 200
escaped characters of that reply: no prompt, parsed body, error text, account identity or host path.

Beside the rows, ``publications.jsonl`` is the published-verdict ledger (#902): one line per verdict the recorder
published on a PR (task id, repository, PR, head, verdict, GitHub's comment time). Its one writer is
``record_cf_verdict`` under the same ``rows.lock``; the statistics count a review's verdict only when the ledger
names it.

``finding-outcomes.jsonl`` is the finding-outcome ledger, under the same lock with the same one writer: one line per
author label of a blocking finding of a published review (``fixed``, ``wrong``, ``invented``, ``no_live_trigger``,
``out_of_scope``, ``duplicate``; the evidence line as its SHA-256 only) and one per later review that contested a
label.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from lu_machinery.agent_runtime.attempt_boundary import CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS
from lu_machinery.api.opsec_scan import scan_text
from lu_machinery.common import json_depth
from lu_machinery.common.git_credentials import _is_valid_slug as _is_selector_slug
from lu_machinery.common.task_store_paths import read_cached_task_record, tasks_dir
from lu_machinery.dispatcher.admission import review_typed
from lu_machinery.dispatcher.types import NEVER_STARTED, StartRefusal
from lu_machinery.fleet_comms.review_publication import (
    BLOCKING_PRIORITIES,
    ReviewPublicationError,
    normalize_verdict,
)
from lu_machinery.model_families import canonical_cursor_model
from lu_machinery.orchestration.fleet_repos import (
    FleetRepo,
    clear_repository_cache,
    load_fleet_repos,
    repository_identity,
)
from lu_machinery.orchestration.task_record_store import REVIEW_TASK_FLAG_FIELDS, iter_task_records
from lu_machinery.review.findings import EVIDENCED_OUTCOME_LABELS, FINDING_OUTCOME_LABELS
from lu_machinery.review.model_catalog import VARIANT_SUFFIX, canonical_model_id, load_model_catalog
from lu_machinery.review.review_contract import SCHEMA_VERSION, ContractError, parse_reviewer_json
from lu_machinery.review.verdict_parser import (
    checked_findings_payload,
    declares_incomplete_review,
    unsupported_verdict_tokens,
)
from lu_machinery.secret_redactor import redact_text

# Deepest JSON this module reads (json_depth): its own model-stats rows.
_JSON_MAX_DEPTH = 128

ROW_SCHEMA = "model-stats-row.v1"
ROWS_DIR = "model-stats"
ROWS_FILE = "rows.jsonl"
LOCK_FILE = "rows.lock"
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_SAMPLE_CHARS = 200
# A reply is scanned whole before any of it is shown; a longer one gives no sample, so a scan stays short.
MAX_SAMPLE_SCAN_BYTES = 16 * 1024
ACTIVE = frozenset({"spawning", "running"})
VERDICTS = ("correct", "incorrect", "uncertain", "unparsed")
UNPARSED_REASONS = ("missing_result", "too_big", "bad_format", "bad_fields")
NO_DELIVERABLE_CAUSES = ("outside_owned_paths", "scope_breaker_stop", "no_commits_or_unknown", "deadline", "other")
_RECORDED_VERDICTS = {"APPROVED": "correct", "CHANGES_REQUESTED": "incorrect", "BLOCKED": "uncertain"}
PRIORITIES = ("P0", "P1", "P2", "P3")
_ESCAPE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_NAMED_ESCAPES = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
_FIELD_CONTRACT_ERRORS = (
    "review_root_must_be_object",
    "schema_violation:",
    "duplicate_finding_ids",
    "invalid_line_range:",
    "review_envelope_verdict_mismatch",
    "review_envelope_invalid_verdict:",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PATH_DIGEST = re.compile(r"[0-9a-f]{16}\Z")
UNSET = "unset"
UNKNOWN_EFFORT = "unknown"
# Native Claude harness aliases that name a family but no version; stored as recorded, never given one here (the
# statistics map one to a catalog id only where the rows make it unambiguous).
NATIVE_ALIASES = ("opus", "sonnet", "haiku", "fable")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}\Z")
# A catalog id may contain one slash (``kimi-code/k3-256k``). Two slashes, a leading slash or ``..`` still fail,
# so a path cannot pass as a model name.
_MODEL_LABEL = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9._:\-]{0,63})?\Z")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
# The dispatcher's task-id grammar (dispatcher.store.record_path); a legacy record's id is not checked on read.
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_EFFORT_SUFFIX = re.compile(rf"-({VARIANT_SUFFIX.pattern})\Z")
_RETIRED_SUBSCRIPTION_MODEL = re.compile(r"(?<![a-z0-9])(?:glm|kimi)(?:cc)?(?![a-z])", re.I)
# Plan-metered pools only; an API-key account label names no allowance and is never stored.
POOLS = frozenset({*(f"claude:{label}" for label in CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS),
                   "codex:a", "codex:b", "agy_claude", "gemini", "cursor"})

_log = logging.getLogger(__name__)

_REFUSAL_CODES = frozenset(StartRefusal)
_PRE_START_ERRORS = {
    "CLAUDE_ACCOUNT_REFUSED": StartRefusal.ACCOUNT,
    "fallback_adoption_unavailable": StartRefusal.PLACEMENT,
    "settlement_registry_unavailable": StartRefusal.SETUP,
    "task_scratch_unavailable": StartRefusal.SETUP,
    "read_only_scratch_unavailable": StartRefusal.SETUP,
    "output schema binding invalid": StartRefusal.SETUP,
    "read_only_unenforceable": StartRefusal.BOUNDARY,
    "write_boundary_refused": StartRefusal.BOUNDARY,  # no wrapper PID and no exit code: launcher construction
    "review_attempt_refused": StartRefusal.REVIEW_SETUP,
    "MODEL_PAUSED": StartRefusal.HEADROOM,
    "MODEL_HEADROOM": StartRefusal.HEADROOM,
    "MODEL_HEADROOM_USAGE_UNKNOWN": StartRefusal.HEADROOM,
    "MODEL_HEADROOM_MONITOR_UNAVAILABLE": StartRefusal.HEADROOM,
    "MODEL_ROLE_BANNED": StartRefusal.ELIGIBILITY,
    "MODEL_ROLE_REQUIRED": StartRefusal.ELIGIBILITY,
    "MODEL_ROLE_POLICY_INVALID": StartRefusal.ELIGIBILITY,
    "MODEL_ROLE_TRIAL_SHADOW_REQUIRED": StartRefusal.ELIGIBILITY,
}


def start_refusal(record: Mapping[str, Any], counts: tuple[int, int, int]) -> str | None:
    """Use the typed dispatcher reason, or require pre-start evidence from a legacy record."""
    status = record.get("status")
    if status not in ("failed", "rate_limited", "crashed") or any(counts):
        return None
    if "never_started_reason" in record:
        # A modern settlement can rule out a refusal even when its last launch exited 125.
        reason = record["never_started_reason"]
        return reason if isinstance(reason, str) and reason in _REFUSAL_CODES else None
    if status != "failed" or record.get("result_sha256") or record.get("resolved_model_known") is True:
        return None

    error = record.get("error")
    code = error.partition(":")[0] if isinstance(error, str) else None
    exit_code = record.get("returncode")
    if exit_code == 125:
        # 125 can also be the provider's exit or a boundary interrupted after launch.
        boundary = record.get("write_boundary" if code == "write_boundary_refused" else "read_only_boundary")
        if (code in ("write_boundary_refused", "agy_read_only_boundary")
                and isinstance(boundary, Mapping) and boundary.get("status") == "refused"):
            return StartRefusal.BOUNDARY
    if code == "agy_model_unavailable":
        return StartRefusal.MODEL_UNAVAILABLE  # the AGY parser proved a clean, zero-turn model selection error
    if exit_code is not None or record.get("provider_pgid") is not None:
        return None
    launch_code = record.get("returncode_reason")
    if launch_code in ("dispatch_fallback_refused", "dispatch_isolation_failed", "worker_spawn_failed"):
        return StartRefusal.PLACEMENT
    if launch_code == "spawn_setup_failed":
        return StartRefusal.SETUP
    if isinstance(error, str) and error.startswith("formal attempt filesystem boundary refused: prepare:"):
        return StartRefusal.BOUNDARY
    return _PRE_START_ERRORS.get(code) if code is not None else None


def label(value: object, default: str = "unknown") -> str:
    if isinstance(value, str) and _LABEL.fullmatch(value) and ".." not in value:
        return value
    return default


def model_label(value: object, default: str = "unknown") -> str:
    """A model id, including a catalog id that contains one slash."""
    if isinstance(value, str) and _MODEL_LABEL.fullmatch(value) and ".." not in value:
        return value
    return default


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError):  # unparseable, or outside the datetime range once moved to UTC
        return None


def tokens(usage: object) -> tuple[tuple[int, int, int], bool]:
    """Normalize fresh input, cache reads and output, and say whether all three are known."""
    if not isinstance(usage, Mapping):
        return (0, 0, 0), False

    valid = True

    def n(mapping: Mapping[str, Any], key: str, *, optional: bool = False) -> int:
        nonlocal valid
        value = mapping.get(key, 0 if optional else None)
        if _count_value(value):
            return cast(int, value)
        valid = False
        return 0

    u = usage
    if "cache_read_input_tokens" in u:  # Claude
        counts = (n(u, "input_tokens") + n(u, "cache_creation_input_tokens", optional=True),
                  n(u, "cache_read_input_tokens"), n(u, "output_tokens"))
    elif "cached_input_tokens" in u:  # Codex: input includes cache reads, output includes reasoning
        fresh, cached = n(u, "input_tokens"), n(u, "cached_input_tokens")
        counts = (max(0, fresh - cached), cached, n(u, "output_tokens"))
        valid = valid and fresh >= cached
    elif "inputTokens" in u:  # Cursor
        counts = (n(u, "inputTokens") + n(u, "cacheWriteTokens"), n(u, "cacheReadTokens"),
                  n(u, "outputTokens") + n(u, "reasoningTokens", optional=True))
    elif "cache_read_tokens" in u:  # AGY
        counts = (n(u, "input_tokens"), n(u, "cache_read_tokens"),
                  n(u, "output_tokens") + n(u, "thinking_tokens", optional=True))
    elif isinstance(cache := u.get("cache"), Mapping):  # OpenCode
        counts = (n(u, "input") + n(cache, "write", optional=True), n(cache, "read"),
                  n(u, "output") + n(u, "reasoning", optional=True))
    else:
        return (0, 0, 0), False
    if not all(_count_value(value) for value in counts):
        return (0, 0, 0), False
    return counts, valid


@lru_cache(maxsize=1)
def model_catalog_reading() -> dict[str, Any] | None:
    """The model catalog, or None when it cannot be read (models are then stored and counted as recorded).

    One reading, kept until ``refresh_readings``: every statistics consumer of the catalog reads it here, so within
    one collection they all see the same catalog, or all see none.
    """
    try:
        return load_model_catalog()
    except Exception:  # no catalog: models are stored as recorded
        return None


def current_catalog_model_ids(catalog: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Non-retired catalog IDs for display, or None when the catalog is unavailable."""
    if catalog is None:
        return None
    return frozenset(model_id for model_id, info in catalog["models"].items()
                     if info.get("lifecycle") != "retired" and model_label(model_id, ""))


@lru_cache(maxsize=256)
def retired_subscription_model(value: str) -> bool:
    """GLM and Kimi labels, including catalog aliases and pre-catalog ledger spellings."""
    return bool(_RETIRED_SUBSCRIPTION_MODEL.search(value) or _RETIRED_SUBSCRIPTION_MODEL.search(model_id(value)))


def _spelling(raw: object) -> str:
    """A model spelling without its bracketed context suffix (``claude-opus-5-5[1m]``)."""
    return raw.split("[", 1)[0].strip() if isinstance(raw, str) else ""


def model_id(raw: object) -> str:
    """The catalog id a spelling names, else the spelling itself; never a family or a guessed version.

    A bare harness alias (``opus``) names no version, so it stays ``opus``. A Cursor display name maps through
    ``canonical_cursor_model`` first, and only for the spellings that function admits. The catalog match runs on
    the raw spelling: a harness prefix (``deepseek-direct/deepseek-v4-flash``) or a catalog id that itself
    contains a slash (``kimi-code/k3-256k``) used to die in the label grammar and be stored as ``unknown``.
    """
    spelling = _spelling(raw)
    if not spelling:
        return "unknown"
    displayed = canonical_cursor_model(spelling)
    if displayed:
        spelling = displayed
    catalog = model_catalog_reading()
    if catalog is not None and (matched := canonical_model_id(spelling, catalog)):
        return model_label(matched)
    return model_label(spelling)


def concrete_model(record: Mapping[str, Any]) -> str:
    """The provider-observed model when the runtime attested one, else the requested model's catalog id.

    The attested spelling goes through ``model_id`` directly. A display name the label grammar would reject
    (a space, or one slash) is still a catalog id when the map admits it, and stays ``unknown`` when it does not.
    The requested model is not a fallback for an attestation the catalog does not name.
    """
    if record.get("resolved_model_known") is True:
        return model_id(record.get("resolved_model"))
    return model_id(record.get("model"))


def _options(record: Mapping[str, Any]) -> Mapping[str, Any]:
    options = record.get("options")
    return options if isinstance(options, Mapping) else {}


def variant_effort(raw: object) -> str | None:
    """The suffix of a catalog model selector, such as ``gemini-4-neon-medium``."""
    spelling = _spelling(raw).lower()
    match = _EFFORT_SUFFIX.search(spelling)
    if match and model_id(spelling) not in {spelling, "unknown"}:
        return match.group(1)
    return None


# Harnesses that pass an explicit effort flag separately from the model selector.
# For OpenCode, Model Stats uses the requested flag effort only for a matching SubQ Savant attestation.
# DeepSeek routes pass the requested effort via --variant, or use the endpoint default when omitted.
_EFFORT_FLAG_AGENTS = frozenset({"claude", "codex", "muse"})


def _newest_claude_id(family: str) -> str | None:
    """The concrete model a native Claude alias names in the current catalog reading."""
    catalog = model_catalog_reading()
    models = catalog.get("models") if isinstance(catalog, Mapping) else None
    if not isinstance(models, Mapping):
        return None
    prefix = f"claude-{family}-"

    def version(mid: str) -> tuple[int, ...]:
        return tuple(int(part) for part in mid[len(prefix):].split("-") if part.isdigit())

    ids = [
        mid
        for mid, info in models.items()
        if isinstance(mid, str)
        and mid.startswith(prefix)
        and isinstance(info, Mapping)
        and info.get("family") == "anthropic"
    ]
    return max(ids, key=version) if ids else None


def _uses_effort_flag(record: Mapping[str, Any]) -> bool:
    raw_agent = record.get("agent")
    if not isinstance(raw_agent, str):
        return False
    agent = raw_agent.strip().lower()
    if agent in _EFFORT_FLAG_AGENTS:
        return True
    if agent == "opencode":
        provider = record.get("provider") if record.get("provider") is not None else _options(record).get("provider")
        return isinstance(provider, str) and provider.strip().lower() == "subq"
    return False


def _observed_matches_request(record: Mapping[str, Any], observed_id: str, requested_id: str) -> bool:
    if requested_id == "unknown" or observed_id.casefold() in NATIVE_ALIASES:
        return False
    if observed_id == requested_id:
        return True
    raw_agent = record.get("agent")
    if isinstance(raw_agent, str) and raw_agent.strip().lower() == "claude":
        alias = requested_id.casefold()
        if alias in NATIVE_ALIASES:
            target = _newest_claude_id(alias)
            return target is not None and observed_id == target
    return False


def effective_effort(record: Mapping[str, Any]) -> str:
    """Use the runtime selector when observed, then a matching selector or verified flag effort."""
    observed_known = record.get("resolved_model_known") is True
    observed = _spelling(record.get("resolved_model")) if observed_known else ""
    requested = record.get("model")
    if level := variant_effort(observed):
        return level
    if observed_known:
        observed_id = model_id(observed)
        if observed_id.casefold() in {"unknown", "unrecognized"}:
            return UNSET
        requested_id = model_id(requested)
        req_level = variant_effort(requested)
        if req_level is not None:
            # A mapped Cursor display name (such as "Grok 4.7 256K High") carries the variant as a
            # trailing space-separated word rather than a hyphen suffix; any other bare attestation
            # or a different attested model never inherits the requested selector's effort.
            if (canonical_cursor_model(observed) != observed and observed_id == requested_id
                    and re.search(rf"[ \t]{re.escape(req_level)}\Z", observed, re.ASCII | re.IGNORECASE)):
                return req_level
            return UNSET
        # Require positive evidence before inheriting a request effort flag on an attested run:
        # the harness must pass a separate effort flag and the attested model must match the request.
        if not (_uses_effort_flag(record) and _observed_matches_request(record, observed_id, requested_id)):
            return UNSET
    elif level := variant_effort(requested):
        return level
    options = _options(record)
    for value in (record.get("effort"), options.get("effort")):
        if isinstance(value, str) and value.strip():
            return label(value.strip().lower())
    return UNSET


def comparison_effort(value: object) -> str:
    """A recorded effort level, or the common bucket for unreported and invalid levels."""
    effort = value.strip().lower() if isinstance(value, str) else ""
    return effort if VARIANT_SUFFIX.fullmatch(effort) else UNKNOWN_EFFORT


def _review_flags(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``record`` whose empty review flags are filled from its options.

    Precedence for each :data:`REVIEW_TASK_FLAG_FIELDS` name is a non-empty top-level value,
    then the same key on options. ``review_risk`` also accepts ``options.risk`` when
    ``options.review_risk`` is empty. A present null is empty: ``dict.get``'s default is not.
    """
    subject = dict(record)
    options = _options(record)
    for name in REVIEW_TASK_FLAG_FIELDS:
        if subject.get(name) in (None, False, "", []):
            value = options.get(name)
            if value in (None, False, "", []) and name == "review_risk":
                value = options.get("risk")
            if value not in (None, False, "", []):
                subject[name] = value
    return subject


def _typed_review(record: Mapping[str, Any]) -> bool:
    """Whether ``review_typed`` types this task, including a flag stored only on its options."""
    return review_typed(_review_flags(record))


def _validated_review(review: Mapping[str, Any] | None) -> bool:
    """A parsed verdict is review evidence. An unparsed or missing reply is not."""
    if not isinstance(review, Mapping):
        return False
    verdict = review.get("verdict")
    return isinstance(verdict, str) and verdict in VERDICTS[:3]


def role_of(record: Mapping[str, Any], review: Mapping[str, Any] | None = None) -> str:
    """Review only when the dispatch classifier types the task, or its reply is a validated review.

    Read-only mode and a task id that contains ``review`` are not a review. A validated reply
    is review evidence on its own, whichever mode the record has. ``review`` is the parsed
    reply from :func:`_review`, when the caller has one.
    """
    if _typed_review(record) or _validated_review(review):
        return "review"
    return "write"


@lru_cache(maxsize=1)
def _fleet_repo_visibility() -> tuple[frozenset[str], dict[str, str], str | None]:
    """(public_keys, github_slug_to_key, default_key); fail closed to empty when unreadable."""
    try:
        repos = load_fleet_repos()
    except Exception:  # fail closed: an unreadable repo catalog admits no reply samples
        return frozenset(), {}, None
    public_keys = frozenset(key for key, repo in repos.items() if repo.role.startswith("public-"))
    by_github = repository_keys(repos)
    default_key = next((key for key, repo in repos.items() if repo.default), None)
    return public_keys, by_github, default_key


@lru_cache(maxsize=1)
def _public_data_classes() -> frozenset[str]:
    """Exact catalogued public data classes (``store: true``); fail closed to empty when unreadable."""
    catalog = model_catalog_reading()
    models = catalog.get("models") if isinstance(catalog, Mapping) else None
    if not isinstance(models, Mapping):
        return frozenset()
    admitted: set[str] = set()
    for info in models.values():
        classes = info.get("data_classes") if isinstance(info, Mapping) else None
        if isinstance(classes, Mapping):
            admitted.update(
                name for name, allowed in classes.items()
                if isinstance(name, str) and allowed is True
            )
    return frozenset(admitted)


def _repo_is_public(repo: object) -> bool:
    public_keys, _, _ = _fleet_repo_visibility()
    return isinstance(repo, str) and repo in public_keys


def _record_allows_sample(record: Mapping[str, Any]) -> bool:
    """Whether a failing reply from ``record`` may give a bounded stats sample (exact allowlists, fail closed)."""
    public_keys, by_github, default_key = _fleet_repo_visibility()
    if not public_keys:
        return False
    options = _options(record)
    declared_repos = [value for value in (record.get("repo"), options.get("repo")) if value is not None]
    resolved_repo: str | None = None
    for value in declared_repos:
        if not isinstance(value, str) or value not in public_keys:
            return False
        if resolved_repo is not None and value != resolved_repo:
            return False
        resolved_repo = value
    slugs = [
        value for value in (
            record.get("repository"), record.get("repository_id"),
            options.get("repository"), options.get("repository_id"),
        )
        if value is not None
    ]
    for slug in slugs:
        if not isinstance(slug, str) or not slug.isascii():
            return False
        slug_key = by_github.get(slug.strip().lower())
        if slug_key is None or slug_key not in public_keys:
            return False
        if resolved_repo is not None and slug_key != resolved_repo:
            return False
        resolved_repo = slug_key
    if resolved_repo is None and default_key not in public_keys:
        return False
    declared_classes = [value for value in (record.get("data_class"), options.get("data_class")) if value is not None]
    if declared_classes:
        public_classes = _public_data_classes()
        if not public_classes:
            return False
        first_class: str | None = None
        for value in declared_classes:
            if not isinstance(value, str) or value not in public_classes:
                return False
            if first_class is not None and value != first_class:
                return False
            first_class = value
    return True


def _needs_escape(ch: str) -> bool:
    return ch in _NAMED_ESCAPES or unicodedata.category(ch) in _ESCAPE_CATEGORIES


def _escape_char(ch: str) -> str:
    if ch in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[ch]
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _escape_sample(prefix: str) -> str:
    parts: list[str] = []
    used = 0
    for ch in prefix:
        piece = _escape_char(ch) if _needs_escape(ch) else ch
        if used + len(piece) > MAX_SAMPLE_CHARS:
            break
        parts.append(piece)
        used += len(piece)
    return "".join(parts)


def _sample_text_cleared(text: str) -> bool:
    """Reject samples carrying OPSEC tokens (paths, IPs, emails, SSH) or secret/credential patterns."""
    return not scan_text(text) and redact_text(text) == text


def _sample(raw: bytes) -> str | None:
    """First ``MAX_SAMPLE_CHARS`` escaped characters of a failing reply when cleared for stats, else None.

    The scans run over the whole reply as read, never the cut prefix alone: a credential or host detail that starts
    in the prefix and ends past the cut is found in full, and any match that starts in the prefix withholds it.
    """
    if len(raw) > MAX_SAMPLE_SCAN_BYTES:
        return None
    text = raw.decode("utf-8", errors="replace")
    prefix = text[:MAX_SAMPLE_CHARS]
    redacted = redact_text(text) or ""
    if any(finding.start < len(prefix) for finding in scan_text(text)) or redacted[:len(prefix)] != prefix:
        return None
    escaped = _escape_sample(prefix)
    return escaped if _sample_text_cleared(prefix) and _sample_text_cleared(escaped) else None


def _unparsed(reason: str, sample: str | None = None) -> dict[str, Any]:
    return {"verdict": "unparsed", "priorities": None, "unparsed_reason": reason, "unparsed_sample": sample}


def _sidecar_path(path: Path, record: Mapping[str, Any]) -> Path | None:
    """Derive the owning ``.result`` sidecar from ``path.stem`` and reject conflicting/malformed declarations."""
    expected_name = f"{path.stem}.result"
    declared = record.get("result_file")
    if declared is not None:
        if not isinstance(declared, str) or not declared.endswith(".result"):
            return None
        declared_name = Path(declared).name
        if declared_name != expected_name:
            task_id = record.get("task_id")
            stamped_archive = (
                isinstance(task_id, str)
                and path.stem.startswith(f"{task_id}.")
                and path.stem.endswith(".archived")
                and declared_name == f"{task_id}.result"
            )
            if not stamped_archive:
                return None
    return path.with_name(expected_name)


def _result(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Verdict and finding priorities from the record's bound ``.result`` sidecar, or a typed unparsed cause.

    Only a regular file, opened without following a link, whose bytes match the record's ``result_sha256`` (when
    declared) is a result. Anything else (absent, a symlink, directory, socket or FIFO, unreadable, replaced bytes)
    is ``missing_result`` with no sample. A file past ``MAX_RESULT_BYTES`` is not read whole: ``too_big``, no sample.
    """
    sidecar = _sidecar_path(path, record)
    expected = record.get("result_sha256")
    if sidecar is None or (expected is not None and not (isinstance(expected, str) and _SHA256.fullmatch(expected))):
        return _unparsed("missing_result")
    try:
        fd = os.open(sidecar, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return _unparsed("missing_result")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _unparsed("missing_result")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_RESULT_BYTES + 1)
    except OSError:
        return _unparsed("missing_result")
    finally:
        os.close(fd)
    if len(raw) > MAX_RESULT_BYTES:
        return _unparsed("too_big")
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        return _unparsed("missing_result")
    return review_summary(raw, allow_sample=_record_allows_sample(record),
                          allow_markdown=record.get("require_review_verdict") is not True)


def _recorded_review(verdict: object) -> dict[str, Any] | None:
    """A structured verdict record does not attest a findings count."""
    try:
        return {"verdict": _RECORDED_VERDICTS[normalize_verdict(verdict)], "priorities": None}
    except ReviewPublicationError:
        return None


def path_digest(path: str) -> str:
    """The stored form of a finding's repository-relative path: equal paths compare equal, the path never shows."""
    return hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def review_summary(raw: bytes, *, allow_sample: bool = False, allow_markdown: bool = False) -> dict[str, Any]:
    """Read structured review output through the review contract, retaining only counts, verdict, or parse cause.

    A ``code-review-findings.v1`` reply also keeps the path digest of each blocking (P0-P1) finding's location, so
    the statistics can tell whether the author's next commits touched that file.
    """
    def fail(reason: str) -> dict[str, Any]:
        return _unparsed(reason, _sample(raw) if allow_sample else None)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return fail("bad_format")
    try:
        payload = parse_reviewer_json(text)
    except ContractError as exc:
        if allow_markdown and str(exc).startswith((
            "non_json_or_chatter:", "review_envelope_invalid_headers", "review_envelope_requires_one_json_block",
            "review_envelope_invalid_json_fence",
        )):
            # The existing recorder checks visibility and refuses conflicting
            # tokens or malformed findings blocks in historical text replies.
            from lu_machinery.review import record_cf_verdict  # noqa: PLC0415  The recorder also loads these rows.

            try:
                if not unsupported_verdict_tokens(text):
                    recorded_verdict = record_cf_verdict.normalize_verdict(text)
                    return {"verdict": _RECORDED_VERDICTS[recorded_verdict], "priorities": None}
            except (record_cf_verdict.RecordError, ValueError, RecursionError):
                pass
        reason = "bad_fields" if str(exc).startswith(_FIELD_CONTRACT_ERRORS) else "bad_format"
        return fail(reason)
    except (ValueError, RecursionError, TypeError):
        return fail("bad_format")
    try:
        envelope = text.lstrip(" \t\r\n").startswith("VERDICT:")
        if declares_incomplete_review(text if envelope else "", findings_payloads=(payload,)):
            return fail("bad_fields")
        if payload.get("schema_version") == SCHEMA_VERSION:
            decision, evidence = checked_findings_payload(payload)
            counts = Counter(finding.priority for finding in evidence.findings)
            # From the same findings as the counts, so the reader's check (one path per P0-P1 finding) always holds.
            paths = sorted(path_digest(finding.path) for finding in evidence.findings
                           if finding.priority in BLOCKING_PRIORITIES)
            return {"verdict": _RECORDED_VERDICTS[decision], "priorities": dict(counts), "blocking_paths": paths}
        if "verdict" in payload and "overall" not in payload and "schema_version" not in payload:
            recorded = _recorded_review(payload["verdict"])
            return recorded if recorded is not None else fail("bad_fields")
    except (ContractError, ReviewPublicationError, ValueError, RecursionError, TypeError, KeyError):
        return fail("bad_fields")
    # Older dispatches also recorded the unversioned correctness and priority fields.
    if "schema_version" in payload:
        return fail("bad_fields")
    overall = payload.get("overall")
    verdict = overall.get("correctness") if isinstance(overall, dict) else None
    findings = payload.get("findings", [])
    if verdict not in VERDICTS[:3] or not isinstance(findings, list) or not all(
            isinstance(finding, dict) and finding.get("priority") in PRIORITIES for finding in findings):
        return fail("bad_fields")
    priorities = Counter(finding["priority"] for finding in findings)
    return {"verdict": verdict, "priorities": dict(sorted(priorities.items()))}


def _review(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    reply = _result(path, record)
    if reply["verdict"] != "unparsed":
        return reply
    return _recorded_review(record.get("verdict")) or reply


def _reading(value: object) -> dict[str, float] | str:
    pct = _number(value.get("remaining_pct")) if isinstance(value, Mapping) else None
    return {"remaining_pct": pct} if pct is not None and pct <= 100 else "unknown"


def _allowance(record: Mapping[str, Any]) -> dict[str, Any] | None:
    block = record.get("allowance")
    if not isinstance(block, Mapping):
        return None
    pool = block.get("pool")
    return {"pool": pool if pool in POOLS else None, "start": _reading(block.get("start")),
            "finish": _reading(block.get("finish"))}


def _landing(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The merged delivery the dispatcher observed for this task (``delivery_pr``), PR number and SHA only."""
    sha = record.get("delivery_merge_sha")
    pr = str(record.get("delivery_pr") or "").rpartition("#")[2]
    # merged_delivery proves a merge into the task's base; only a main-based task's merge is a landing.
    if record.get("worktree_base_ref", "origin/main") not in ("origin/main", "main"):
        return None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or not pr.isdigit():
        return None
    return {"pr": int(pr), "merge_sha": sha}


def valid_task_id(value: object) -> bool:
    """Whether ``value`` is a dispatcher task id; any other value is never stored, so no free text enters a row."""
    return isinstance(value, str) and _TASK_ID.match(value) is not None and ".." not in value


def _iso(value: object) -> str | None:
    moment = timestamp(value)
    return moment.isoformat() if moment else None


def _no_deliverable_cause(record: Mapping[str, Any]) -> str | None:
    """Retain the stated cause category, dropping free text such as a list of paths."""
    reason = record.get("no_deliverable_reason") or record.get("error")
    if not isinstance(reason, str) or not reason.strip():
        return None
    code = re.sub(r"[-\s]+", "_", reason.partition(":")[0].strip().casefold())
    if code in {"scope_breaker", "scope_breaker_stop"}:
        return "scope_breaker_stop"
    if code in {"deadline", "hard_deadline", "deadline_exceeded", "process_deadline_exceeded"}:
        return "deadline"
    if code in {"outside_owned_paths", "no_commits_or_unknown"}:
        return code
    return "other"


def summary_row(path: Path, record: Mapping[str, Any], source_mtime_ns: int | None = None) -> dict[str, Any] | None:
    """The summary row of a settled task record, or None for an active or foreign record or an invalid task id.

    ``source_mtime_ns`` is the record file's modification time when it was read: of two rows for one task id, the
    one from the later record version is the current one.
    """
    task_id = record.get("task_id")
    status = record.get("status")
    if not valid_task_id(task_id) or not isinstance(status, str) or status in ACTIVE:
        return None
    counts, usage_known = tokens(record.get("usage"))
    refusal = start_refusal(record, counts)
    options = _options(record)
    # One resolved copy drives typing, class, PR association and reply parsing. A typed review
    # that did not finish has no reply to read. Any other settled task is read once, so a legacy
    # record keeps review attribution only when the reply validates, in either mode.
    resolved = _review_flags(record)
    typed = review_typed(resolved)
    reading = None if refusal or (typed and status != "done") else _review(path, resolved)
    role = role_of(resolved, reading)
    if role == "review":
        task_class = (f"{label(resolved.get('review_profile'), 'code')}/"
                      f"{label(resolved.get('review_risk'))}")
    else:
        task_class = label(record.get("research_task_family"), "unclassified")
    pr = resolved.get("pr")
    head = record.get("pinned_head") or options.get("pinned_head") or record.get("worktree_base_ref")
    author = resolved.get("review_author_model")
    raw_repo = record.get("repo") if record.get("repo") is not None else options.get("repo")
    repo = "public" if raw_repo is None else label(raw_repo, "unknown")
    raw_class = record.get("data_class") if record.get("data_class") is not None else options.get("data_class")
    repository = record.get("repository")
    fresh, cached, output = counts
    # Terminal status still decides whether a verdict is stored. A review that is typed, or
    # whose reply validated, is not reported as missing review evidence.
    review = reading if role == "review" and not refusal and status == "done" else None
    return {
        "schema": ROW_SCHEMA,
        "task_id": task_id,
        "harness": label(record.get("agent")),
        "model": concrete_model(record),
        "effort": effective_effort(record),
        "role": role,
        "class": task_class,
        "status": NEVER_STARTED if refusal else label(status, "other"),
        "never_started_reason": refusal,
        "no_deliverable_cause": _no_deliverable_cause(record) if status == "no_deliverable" and not refusal else None,
        "started_at": _iso(record.get("started_at")),
        "finished_at": _iso(record.get("finished_at")),
        "duration_s": _number(record.get("duration_s")),
        "tokens": {"fresh_input": fresh, "cached_input": cached, "output": output},
        "usage_known": usage_known or refusal is not None,
        "implementation": role == "write" and record.get("mode") == "workspace-write"
        and record.get("research_role") == "implementation",
        "allowance": None if refusal else _allowance(record),
        "review": review,
        "repo": repo,
        # The GitHub owner/name the task ran on, so a reader can bind a publication without a fleet catalog.
        "repository": repository if isinstance(repository, str) and REPOSITORY_SLUG.fullmatch(repository) else None,
        "data_class": None if raw_class is None else label(raw_class),
        "pr": int(pr) if isinstance(pr, int | str) and not isinstance(pr, bool) and str(pr).isdigit() else None,
        "pinned_head": head if isinstance(head, str) and _SHA.fullmatch(head) else None,
        "author_model": model_id(author) if author else None,
        "landing": _landing(record),
        "source_mtime_ns": source_mtime_ns,
    }


def rows_path(batch_state_dir: Path) -> Path:
    return batch_state_dir / ROWS_DIR / ROWS_FILE


# A settlement never waits longer than this for the rows lock; the backfill repairs a row it skipped.
SETTLEMENT_LOCK_WAIT_S = 5.0
_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


def _open(path: Path, flags: int) -> int:
    """Open without following a symlink, so a planted link never redirects a write or a read."""
    return os.open(path, flags | _NOFOLLOW, 0o600)


@contextmanager
def _locked(batch_state_dir: Path, wait_s: float | None = None) -> Iterator[Path]:
    """Hold ``rows.lock``; ``wait_s`` bounds the wait (TimeoutError), None waits as long as it takes."""
    directory = batch_state_dir / ROWS_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise NotADirectoryError("the model-stats directory is not a directory")
    fd = _open(directory / LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        if wait_s is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("the model-stats rows lock is busy") from None
                    # flock has no timeout; this poll interval bounds a settlement's wait to wait_s.
                    time.sleep(0.05)  # nosemgrep: python.lang.best-practice.arbitrary-sleep
        yield directory / ROWS_FILE
    finally:
        os.close(fd)


def _line(row: Mapping[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _append_line(path: Path, line: bytes) -> None:
    """Append one whole line; a fragment an earlier failed append left is closed first, so it stays one bad line."""
    fd = _open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT)
    try:
        size = os.fstat(fd).st_size
        open_fragment = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        _write_all(fd, (b"\n" if open_fragment else b"") + line)
    finally:
        os.close(fd)


def append_row(batch_state_dir: Path, row: Mapping[str, Any] | Callable[[], Mapping[str, Any] | None],
               wait_s: float | None = None) -> None:
    """Append one whole row line under ``rows.lock``.

    ``row`` may be a function, called under the lock: the sidecar it reads is then never older than a row another
    writer has already stored.
    """
    with _locked(batch_state_dir, wait_s) as path:
        line = row() if callable(row) else row
        if line is not None:
            _append_line(path, _line(line))


def record_settled(path: Path, record: Mapping[str, Any], batch_state_dir: Path, source_mtime_ns: int) -> None:
    """Append the row of a just-written settled record. Never raises: the record's status stays as written.

    ``source_mtime_ns`` is the modification time of the file the writer renamed into place (its content's version).
    """
    try:
        append_row(batch_state_dir, lambda: summary_row(path, record, source_mtime_ns), SETTLEMENT_LOCK_WAIT_S)
    except Exception as exc:
        _log.warning("model-stats summary row not written for a settled task: %s", type(exc).__name__)


# The published-verdict ledger (#902): one line per verdict the recorder published on a PR as the review of record,
# in ``<batch_state>/model-stats/publications.jsonl``. Its one writer is ``record_cf_verdict`` (each ``record`` and
# the one-shot ``import-publications``); the statistics read it and never reach GitHub. A review without a line is
# of unknown publication. A line of a dual-slug repository also carries ``fleet_repo_selector``, the selector receipt
# (value and sha256) the writer resolved the repository with (SELECTOR-DURABLE-RECEIPTS); lines without it (single-slug
# rows, lines written before it) read as having none. The receipt is provenance, never authority: no reader may use
# it to authorize a write, and it is no part of what makes two lines the same publication.
PUBLICATION_SCHEMA = "review-publication.v1"
PUBLICATIONS_FILE = "publications.jsonl"
PUBLISHED_VERDICTS = frozenset({"APPROVED", "CHANGES_REQUESTED", "BLOCKED"})
_PUBLICATION_KEYS = frozenset({"schema", "task_id", "repository", "pr", "head", "verdict", "published_at"})
_PUBLICATION_SELECTOR = "fleet_repo_selector"
REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\Z")


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose member names repeat is refused (ValueError): the decoder would keep only the last value."""
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("duplicate JSON object member")
    return members


def _load_line(line: bytes) -> Any:
    """One ledger or row line, refusing repeated member names (ValueError) and deep nesting (RecursionError)."""
    return json_depth.loads(line, max_depth=_JSON_MAX_DEPTH, object_pairs_hook=_unique_members)


def _selector_ok(receipt: object) -> bool:
    """The optional receipt uses the selector loader's slug grammar and a lowercase SHA-256."""
    if receipt is None:
        return True
    if not isinstance(receipt, dict) or receipt.keys() != {"selector_value", "selector_sha256"}:
        return False
    digest = receipt["selector_sha256"]
    return _is_selector_slug(receipt["selector_value"]) and isinstance(digest, str) and _SHA256.fullmatch(digest) is not None


def _publication_ok(entry: object) -> bool:
    if not isinstance(entry, dict) or entry.keys() - {_PUBLICATION_SELECTOR} != _PUBLICATION_KEYS:
        return False
    if not _selector_ok(entry.get(_PUBLICATION_SELECTOR)):
        return False
    pr, repository = entry["pr"], entry["repository"]
    return (entry["schema"] == PUBLICATION_SCHEMA and valid_task_id(entry["task_id"])
            and isinstance(repository, str) and REPOSITORY_SLUG.fullmatch(repository) is not None
            and _count_value(pr) and pr > 0 and _sha(entry["head"])
            and isinstance(entry["verdict"], str) and entry["verdict"] in PUBLISHED_VERDICTS
            and timestamp(entry["published_at"]) is not None)


def publication(*, task_id: str, repository: str, pr: int, head: str, verdict: str,
                published_at: object, fleet_repo_selector: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One ledger line: which review task's verdict was published on which PR head, and when GitHub stored it.

    ``published_at`` is GitHub's comment time as read back (any value; only an ISO timestamp is accepted).
    ``fleet_repo_selector`` is the selector receipt the writer resolved a dual-slug repository with; the line carries
    it only when it is not None. Raises ValueError when a field has the wrong shape, so a malformed line is never
    written.
    """
    entry: dict[str, Any] = {"schema": PUBLICATION_SCHEMA, "task_id": task_id, "repository": repository, "pr": pr,
                             "head": head, "verdict": verdict, "published_at": published_at}
    if fleet_repo_selector is not None:
        if not isinstance(fleet_repo_selector, Mapping):
            raise ValueError("publication field has the wrong shape")
        receipt: dict[str, str] = {}
        for key, value in dict(fleet_repo_selector).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("publication field has the wrong shape")
            # Match the JSON representation without invoking overridden methods on a string subclass.
            name = str.__str__(key)
            if name in receipt:
                raise ValueError("publication field has the wrong shape")
            receipt[name] = str.__str__(value)
        entry[_PUBLICATION_SELECTOR] = receipt
    if not _publication_ok(entry):
        raise ValueError("publication field has the wrong shape")
    return entry


def _publication_lines(path: Path, counts: Counter[str]) -> list[dict[str, Any]]:
    try:
        fd = _open(path, os.O_RDONLY)
    except FileNotFoundError:
        counts["missing"] += 1
        return []
    entries = []
    with os.fdopen(fd, "rb") as stream:
        for line in stream:
            try:
                entry = _load_line(line)
            except (ValueError, RecursionError):
                entry = None
            if not _publication_ok(entry):
                counts["malformed_lines"] += 1
                continue
            entries.append(entry)
    counts["lines"] = len(entries)
    return entries


def publication_key(entry: Mapping[str, Any]) -> tuple[str, int | str, int, str, str]:
    """Identify one task's verdict for one PR head, including across a repository rename."""
    return (entry["task_id"], repository_identity(entry["repository"]), entry["pr"], entry["head"], entry["verdict"])


def publication_keys(batch_state_dir: Path) -> set[tuple[str, int | str, int, str, str]]:
    """The keys of every well-formed ledger line."""
    return {publication_key(e) for e in _publication_lines(batch_state_dir / ROWS_DIR / PUBLICATIONS_FILE, Counter())}


def record_publications(batch_state_dir: Path, entries: Iterable[Mapping[str, Any]],
                        wait_s: float | None = None) -> int:
    """Append, under ``rows.lock``, each entry the ledger does not hold yet (by ``publication_key``); the count added.

    Every entry is checked before anything is written: one malformed entry raises ValueError and writes nothing.
    Each entry is validated from the exact JSON bytes that will be written.
    TimeoutError when ``wait_s`` passes before the lock is free; OSError when the ledger cannot be written.
    """
    checked = []
    for entry in entries:
        try:
            row = dict(entry)
            # JSON encodes integer subclasses as plain integers. The ledger requires a built-in int for PRs.
            if not _count_value(row.get("pr")):
                raise ValueError("publication PR must be an integer in range")
            line = _line(row)
            snapshot = _load_line(line)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("publication field has the wrong shape") from exc
        if not _publication_ok(snapshot):
            raise ValueError("publication field has the wrong shape")
        checked.append((publication_key(snapshot), line))
    with _locked(batch_state_dir, wait_s) as rows_file:
        path = rows_file.with_name(PUBLICATIONS_FILE)
        present = {publication_key(e) for e in _publication_lines(path, Counter())}
        added = 0
        for key, line in checked:
            if key not in present:
                _append_line(path, line)
                present.add(key)
                added += 1
        return added


def read_publications(batch_state_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], Counter[str]]:
    """Each review task's published verdicts, newest first by GitHub's comment time (of two at the same time, the
    later line first, so a history import appended after a newer publication never comes before it), and read
    counts. A reader takes the first entry that matches its row's repository, PR and head. A missing ledger gives
    no entries: every review is then of unknown publication."""
    counts: Counter[str] = Counter()
    by_task: dict[str, list[tuple[datetime, int, dict[str, Any]]]] = {}
    lines = _publication_lines(batch_state_dir / ROWS_DIR / PUBLICATIONS_FILE, counts)
    for index, entry in enumerate(lines):
        by_task.setdefault(entry["task_id"], []).append(
            (cast(datetime, timestamp(entry["published_at"])), index, entry))
    counts["tasks"] = len(by_task)
    return {task: [entry for *_, entry in sorted(found, key=lambda item: item[:2], reverse=True)]
            for task, found in by_task.items()}, counts


OUTCOME_SCHEMA = "review-finding-outcome.v1"
OUTCOMES_FILE = "finding-outcomes.jsonl"
_FINDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_GIT_HEAD_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OUTCOME_REVIEWER_MODEL = "reviewer_model"
_OUTCOME_RUN_KEYS = frozenset({"head", "started_at"})
_OUTCOME_KEYS = {
    "label": frozenset({"schema", "kind", "task_id", "repository", "pr", "finding_id", "label", "evidence_sha256",
                        "recorded_by", "recorded_at"}),
    "contest": frozenset({"schema", "kind", "task_id", "repository", "pr", "finding_id", "contested_by",
                          "recorded_at"}),
}


def _outcome_ok(entry: object, *, require_run_binding: bool = False) -> bool:
    # Every membership test below reads a string first: a list or object in a damaged line is unhashable.
    if not isinstance(entry, dict):
        return False
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in _OUTCOME_KEYS:
        return False
    expected = _OUTCOME_KEYS[kind]
    optional = _OUTCOME_RUN_KEYS | ({_OUTCOME_REVIEWER_MODEL} if kind == "label" else frozenset())
    if not (expected <= entry.keys() <= (expected | optional)):
        return False
    has_head, has_started = "head" in entry, "started_at" in entry
    if has_head != has_started:
        return False
    if require_run_binding and (not has_head or (kind == "label" and _OUTCOME_REVIEWER_MODEL not in entry)):
        return False
    if has_head and not (
        isinstance(entry["head"], str) and _GIT_HEAD_SHA.fullmatch(entry["head"]) is not None
        and timestamp(entry["started_at"]) is not None
    ):
        return False
    if _OUTCOME_REVIEWER_MODEL in entry:
        reviewer = entry[_OUTCOME_REVIEWER_MODEL]
        if not isinstance(reviewer, str) or not reviewer or model_label(reviewer, "") != reviewer or reviewer == "unknown":
            return False
    pr, repository, finding_id = entry["pr"], entry["repository"], entry["finding_id"]
    if not (entry["schema"] == OUTCOME_SCHEMA and valid_task_id(entry["task_id"])
            and isinstance(repository, str) and REPOSITORY_SLUG.fullmatch(repository) is not None
            and _count_value(pr) and pr > 0 and isinstance(finding_id, str) and _FINDING_ID.fullmatch(finding_id)
            and timestamp(entry["recorded_at"]) is not None):
        return False
    if kind == "contest":
        return valid_task_id(entry["contested_by"]) and entry["contested_by"] != entry["task_id"]
    evidence, label = entry["evidence_sha256"], entry["label"]
    return (isinstance(label, str) and label in FINDING_OUTCOME_LABELS
            and (evidence is None or (isinstance(evidence, str) and _SHA256.fullmatch(evidence) is not None))
            and (evidence is not None or label not in EVIDENCED_OUTCOME_LABELS)
            and (entry["recorded_by"] is None or valid_task_id(entry["recorded_by"])))


def finding_outcome(*, kind: str, task_id: str, repository: str, pr: int, finding_id: str, recorded_at: str,
                    head: str | None = None, started_at: str | None = None, reviewer_model: str | None = None,
                    label: str | None = None, evidence: str | None = None, recorded_by: str | None = None,
                    contested_by: str | None = None) -> dict[str, Any]:
    """One outcome-ledger line: the author's ``label`` for one blocking finding of the published review ``task_id``
    (carrying ``head``, ``started_at``, and ``reviewer_model`` at write time), or a later review's ``contest`` of that
    label (carrying the target run's ``head`` and ``started_at``). The evidence line is kept only as its SHA-256: the
    text stays in the PR and the delta brief. Raises ValueError when a field has the wrong shape, so a malformed line
    is never written."""
    entry: dict[str, Any] = {"schema": OUTCOME_SCHEMA, "kind": kind, "task_id": task_id, "repository": repository,
                             "pr": pr, "finding_id": finding_id, "head": head, "started_at": started_at,
                             "recorded_at": recorded_at}
    if kind == "label":
        entry.update(reviewer_model=reviewer_model, label=label, recorded_by=recorded_by,
                     evidence_sha256=None if evidence is None else hashlib.sha256(evidence.encode()).hexdigest())
    else:
        if reviewer_model is not None:
            raise ValueError("finding outcome field has the wrong shape")
        entry["contested_by"] = contested_by
    if not _outcome_ok(entry, require_run_binding=True):
        raise ValueError("finding outcome field has the wrong shape")
    return entry


def _outcome_lines(path: Path, counts: Counter[str]) -> list[dict[str, Any]]:
    try:
        fd = _open(path, os.O_RDONLY)
    except FileNotFoundError:
        counts["missing"] += 1
        return []
    entries = []
    with os.fdopen(fd, "rb") as stream:
        for line in stream:
            try:
                entry = _load_line(line)
            except (ValueError, RecursionError):
                entry = None
            if not _outcome_ok(entry):
                counts["malformed_lines"] += 1
                continue
            entries.append(entry)
    counts["lines"] = len(entries)
    return entries


class OutcomeConflict(ValueError):
    """The finding already has another label: a finding takes one label."""


def record_finding_outcomes(batch_state_dir: Path, entries: Iterable[Mapping[str, Any]],
                            wait_s: float | None = None) -> int:
    """Append, under ``rows.lock``, each entry the outcome ledger does not hold yet; the count added.

    A label line is one per (review task, repository, PR, finding, head, started_at), the repository by its identity
    across a transfer: the same label again adds nothing and keeps the first line and its evidence, and another label
    raises OutcomeConflict before anything is written. Every newly written line must carry ``head`` and
    ``started_at`` (and ``reviewer_model`` on labels); existing legacy lines without them are still read so legacy
    labels remain conflict-protected. A contest line is one per (review task, repository, PR, finding, contesting
    task, head, started_at). Every entry is checked from the exact JSON bytes that will be written: one malformed
    entry raises ValueError and writes nothing. TimeoutError when ``wait_s`` passes first; OSError when the ledger
    cannot be written.
    """
    checked = []
    for entry in entries:
        try:
            row = dict(entry)
            if not _count_value(row.get("pr")):
                raise ValueError("finding outcome PR must be an integer in range")
            line = _line(row)
            snapshot = _load_line(line)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("finding outcome field has the wrong shape") from exc
        if not _outcome_ok(snapshot, require_run_binding=True):
            raise ValueError("finding outcome field has the wrong shape")
        checked.append((snapshot, line))

    def base_key(entry: Mapping[str, Any]) -> tuple[str, str, int | str, int, str, str]:
        return (entry["kind"], entry["task_id"], repository_identity(entry["repository"]), entry["pr"],
                entry["finding_id"], entry.get("contested_by") or "")

    def run_key(entry: Mapping[str, Any]) -> tuple[str, datetime] | None:
        return (entry["head"], cast(datetime, timestamp(entry["started_at"]))) if "head" in entry else None

    def key(entry: Mapping[str, Any]) -> tuple[str, str, int | str, int, str, str, tuple[str, datetime] | None]:
        return (*base_key(entry), run_key(entry))

    with _locked(batch_state_dir, wait_s) as rows_file:
        path = rows_file.with_name(OUTCOMES_FILE)
        present: dict[tuple[str, str, int | str, int, str, str, tuple[str, datetime] | None], dict[str, Any]] = {}
        for existing in _outcome_lines(path, Counter()):
            present.setdefault(key(existing), existing)

        def legacy_match(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
            if snapshot["kind"] != "label":
                return None
            legacy = present.get((*base_key(snapshot), None))
            if legacy is None:
                return None
            # A legacy label recorded before this candidate run started belongs to an earlier run, not this one.
            legacy_recorded = cast(datetime, timestamp(legacy["recorded_at"]))
            candidate_started = cast(datetime, timestamp(snapshot["started_at"]))
            return None if legacy_recorded < candidate_started else legacy

        planned: dict[tuple[str, str, int | str, int, str, str, tuple[str, datetime] | None], Mapping[str, Any]] = {}
        for snapshot, _ in checked:
            exact = key(snapshot)
            held = present.get(exact) or planned.get(exact) or legacy_match(snapshot)
            if held is not None and snapshot["kind"] == "label" and held["label"] != snapshot["label"]:
                raise OutcomeConflict(
                    f"finding {snapshot['finding_id']} of {snapshot['task_id']} is already labelled {held['label']}")
            planned.setdefault(exact, snapshot)
        added = 0
        for snapshot, line in checked:
            exact = key(snapshot)
            if exact not in present and legacy_match(snapshot) is None:
                _append_line(path, line)
                present[exact] = snapshot
                added += 1
        return added


OutcomeFindingKey = tuple[str, int | str, int, str, str, datetime]


def read_finding_outcomes(batch_state_dir: Path) -> tuple[
        dict[OutcomeFindingKey, tuple[str, str, datetime]], dict[OutcomeFindingKey, datetime], Counter[str]]:
    """The attributed run-bound labels (each keyed by (review task, repository identity, PR, finding id, head,
    started_at) to (reviewer model, label, recorded time)), the earliest contest time of each run-bound contested
    finding, and read counts. A missing ledger gives none. Legacy lines written without ``reviewer_model`` or run
    binding (``head`` and ``started_at``) are not malformed and count in ``unattributed_labels`` or
    ``unbound_contests`` without joining another ledger or suppressing another run. FleetRepoError when the fleet
    registry cannot be read."""
    counts: Counter[str] = Counter()
    labels: dict[OutcomeFindingKey, tuple[str, str, datetime]] = {}
    contests: dict[OutcomeFindingKey, datetime] = {}
    for entry in _outcome_lines(batch_state_dir / ROWS_DIR / OUTCOMES_FILE, counts):
        repo_id = repository_identity(entry["repository"])
        moment = cast(datetime, timestamp(entry["recorded_at"]))
        if entry["kind"] == "label":
            reviewer = entry.get(_OUTCOME_REVIEWER_MODEL)
            if reviewer is None or "head" not in entry:
                counts["unattributed_labels"] += 1
                continue
            finding_key: OutcomeFindingKey = (
                entry["task_id"], repo_id, entry["pr"], entry["finding_id"],
                entry["head"], cast(datetime, timestamp(entry["started_at"])),
            )
            labels.setdefault(finding_key, (model_id(reviewer), entry["label"], moment))
        else:
            if "head" not in entry:
                counts["unbound_contests"] += 1
                continue
            finding_key = (
                entry["task_id"], repo_id, entry["pr"], entry["finding_id"],
                entry["head"], cast(datetime, timestamp(entry["started_at"])),
            )
            previous = contests.get(finding_key)
            if previous is None or moment < previous:
                contests[finding_key] = moment
    return labels, contests, counts


def refresh_readings() -> None:
    """Forget every catalog reading this module, the model catalog loader and the fleet registry keep, so their next
    use reads the files.

    The statistics collector calls it once as each collection starts: a catalog that was unreadable, or valid, at
    an earlier collection is never reused, and a failed reading lasts only for the collection that made it.
    """
    load_model_catalog.cache_clear()
    model_catalog_reading.cache_clear()
    retired_subscription_model.cache_clear()
    _fleet_repo_visibility.cache_clear()
    _public_data_classes.cache_clear()
    clear_repository_cache()


def fleet_catalog() -> dict[str, FleetRepo] | None:
    """The fleet repository catalog, or None when it cannot be read (no publication then matches a row)."""
    try:
        return load_fleet_repos()
    except Exception:  # fail closed: an unreadable catalog matches no publication
        return None


def repository_keys(repos: Mapping[str, FleetRepo] | None) -> dict[str, str]:
    """Each registered GitHub ``owner/name``, lowercased, to its fleet repository key (the rows' ``repo``); empty
    without a catalog."""
    return {
        slug.lower(): key
        for key, repo in (repos or {}).items()
        for slug in repo.slugs
        if slug.isascii()
    }


_LABEL_FIELDS = ("harness", "model", "effort", "role", "class", "status", "repo")


def _sanitised(row: dict[str, Any]) -> dict[str, Any]:
    """A row as read, with every label revalidated, so a damaged file never puts free text in a label."""
    for name in _LABEL_FIELDS:
        value = row.get(name)
        if name == "model":
            row[name] = model_label(value)
        elif name == "class" and isinstance(value, str) and value.count("/") == 1:
            row[name] = "/".join(label(part) for part in value.split("/"))
        else:
            row[name] = label(value)
    author = row.get("author_model")
    row["author_model"] = model_label(author) if author is not None else None
    data_class = row.get("data_class")
    row["data_class"] = label(data_class) if data_class is not None else None
    return row


# Every stored number is at most a signed 64-bit integer, so no sum or ratio over any number of rows overflows a
# float or serializes as infinity.
MAX_VALUE = 2**63 - 1


def _count_value(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_VALUE


def _measure(value: object, top: float = MAX_VALUE) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return 0 <= value <= top  # NaN fails both comparisons; an integer of any size compares exactly


def _optional(value: object, check: Any) -> bool:
    return value is None or check(value)


def _reading_ok(value: object) -> bool:
    return value == "unknown" or (isinstance(value, dict) and value.keys() == {"remaining_pct"}
                                  and _measure(value["remaining_pct"], 100))


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _sample_ok(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_SAMPLE_CHARS
        and not any(_needs_escape(ch) for ch in value)
        and _sample_text_cleared(value)
    )


def _review_ok(review: object, *, role: object, status: object, repo: object, data_class: object,
               check_sample_visibility: bool = True) -> bool:
    if review is None:
        return True
    if role != "review" or status != "done" or not isinstance(review, dict):
        return False
    verdict = review.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return False
    priorities = review.get("priorities")
    if not _optional(priorities, lambda p: isinstance(p, dict)
                     and all(k in PRIORITIES and _count_value(n) for k, n in p.items())):
        return False
    if review.keys() == {"verdict", "priorities"}:
        return True
    if review.keys() == {"verdict", "priorities", "blocking_paths"}:
        paths = review["blocking_paths"]
        return (verdict != "unparsed" and isinstance(priorities, dict) and isinstance(paths, list)
                and all(isinstance(p, str) and _PATH_DIGEST.fullmatch(p) for p in paths)
                and len(paths) == sum(priorities.get(p, 0) for p in BLOCKING_PRIORITIES))
    if verdict != "unparsed" or priorities is not None or review.keys() != {
        "verdict", "priorities", "unparsed_reason", "unparsed_sample",
    }:
        return False
    reason = review["unparsed_reason"]
    sample = review["unparsed_sample"]
    if not isinstance(reason, str) or reason not in UNPARSED_REASONS:
        return False
    if reason in ("missing_result", "too_big"):
        return sample is None
    # The writer's eligibility rule again: a public repository, and a catalogued public data class when one is named.
    # Both are catalog facts; a reader that must not depend on a catalog skips them and keeps the sample shape check.
    if check_sample_visibility and (not _repo_is_public(repo) or (
            data_class is not None and (not isinstance(data_class, str) or data_class not in _public_data_classes()))):
        return sample is None
    return _optional(sample, _sample_ok)


def _well_formed(row: Mapping[str, Any], *, check_sample_visibility: bool = True) -> bool:
    """Whether every nested value has the shape ``summary_row`` writes; a damaged row is skipped, never trusted.

    ``check_sample_visibility=False`` reads no catalog: an unparsed-reply sample is checked for its own shape only,
    not for the repository and data-class visibility the writer applied when it stored it.
    """
    tokens_ = row.get("tokens")
    allowance = row.get("allowance")
    review = row.get("review")
    landing = row.get("landing")
    return all((
        all(_optional(row.get(name), lambda v: timestamp(v) is not None) for name in ("started_at", "finished_at")),
        _optional(row.get("duration_s"), _measure),
        _optional(row.get("never_started_reason"), lambda v: isinstance(v, str) and v in _REFUSAL_CODES
                  and row.get("status") == NEVER_STARTED),
        _optional(row.get("no_deliverable_cause"), lambda v: isinstance(v, str) and v in NO_DELIVERABLE_CAUSES
                  and row.get("status") == "no_deliverable"),
        isinstance(tokens_, dict) and tokens_.keys() == {"fresh_input", "cached_input", "output"}
        and all(_count_value(v) for v in tokens_.values()),
        _optional(row.get("usage_known"), lambda v: type(v) is bool),
        _optional(row.get("implementation"), lambda v: type(v) is bool and (not v or row.get("role") == "write")),
        _optional(allowance, lambda a: isinstance(a, dict) and a.keys() == {"pool", "start", "finish"}
                  and (a["pool"] is None or (isinstance(a["pool"], str) and a["pool"] in POOLS))
                  and _reading_ok(a["start"]) and _reading_ok(a["finish"])),
        _review_ok(review, role=row.get("role"), status=row.get("status"), repo=row.get("repo"),
                   data_class=row.get("data_class"), check_sample_visibility=check_sample_visibility),
        _optional(row.get("review_evidence_absent"), lambda flag: flag is True and row.get("role") == "review"
                  and row.get("status") != NEVER_STARTED and (review is None or (
                      isinstance(review, dict) and review.get("verdict") == "unparsed"))),
        _optional(row.get("pr"), _count_value),
        _optional(row.get("repository"), lambda v: isinstance(v, str) and REPOSITORY_SLUG.fullmatch(v) is not None),
        _optional(row.get("pinned_head"), _sha),
        _optional(landing, lambda d: isinstance(d, dict) and d.keys() == {"pr", "merge_sha"}
                  and _count_value(d["pr"]) and _sha(d["merge_sha"])),
        _optional(row.get("source_mtime_ns"), _count_value),
    ))


def read_rows(batch_state_dir: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Every task's latest row (a later line of a task id supersedes an earlier one), and read counts."""
    counts: Counter[str] = Counter()
    latest: dict[str, dict[str, Any]] = {}
    try:
        fd = _open(rows_path(batch_state_dir), os.O_RDONLY)
    except FileNotFoundError:
        counts["missing"] += 1
        return [], counts
    with os.fdopen(fd, "rb") as stream:
        for line in stream:
            try:
                row = _load_line(line)
            except (ValueError, RecursionError):
                counts["malformed_lines"] += 1
                continue
            if (not isinstance(row, dict) or row.get("schema") != ROW_SCHEMA or not valid_task_id(row.get("task_id"))
                    or not _well_formed(row)):
                counts["malformed_lines"] += 1
                continue
            counts["lines"] += 1
            latest[row["task_id"]] = _sanitised(row)
    counts["rows"] = len(latest)
    return list(latest.values()), counts


def _settled_records(tasks_dir: Path, counts: Counter[str]) -> Iterator[tuple[Path, dict[str, Any], int]]:
    seen: set[str] = set()
    for path in iter_task_records(tasks_dir, include_archive=True):
        if path.name.endswith(".write-boundary.json") or ".sandbox." in path.name:
            continue
        try:
            # Stat first: a write after the stat is newer than this read, so its own row outranks this one.
            mtime_ns = os.stat(path).st_mtime_ns
            record = read_cached_task_record(path, tasks_dir=tasks_dir)
        except (OSError, ValueError, TypeError, RecursionError):
            counts["unreadable"] += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            continue
        if not valid_task_id(record["task_id"]):
            counts["invalid_task_id"] += 1
            continue
        if record["task_id"] in seen:  # the hot record outranks an archived copy
            counts["duplicate_task_ids"] += 1
            continue
        seen.add(record["task_id"])
        yield path, record, mtime_ns


def _stale(present: Mapping[str, Any] | None, candidate: Mapping[str, Any]) -> bool:
    """Refresh missing or older rows, or a row of the same record version that its sidecar no longer supports.

    A row shows only what its record and sidecar prove when it is written: nothing is carried over from a stored
    row, so a sidecar that is gone, replaced or unreadable now yields the row it yields on its own.
    """
    if present is None:
        return True
    stored = present.get("source_mtime_ns")
    source = candidate["source_mtime_ns"]
    if not isinstance(stored, int) or stored < source:
        return True
    return stored == source and any(present.get(key) != candidate.get(key) for key in (
        "role", "class", "review", "effort", "pinned_head", "never_started_reason", "no_deliverable_cause",
        "review_evidence_absent", "usage_known", "implementation", "tokens",
    ))


def backfill(tasks_dir: Path, batch_state_dir: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Add a row for every settled record without a current one; idempotent. Records are read outside the lock.

    A new last line supersedes an older record version, or a row whose review its record's sidecar no longer gives.
    """
    counts: Counter[str] = Counter()
    candidates: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for path, record, mtime_ns in _settled_records(tasks_dir, counts):
        row = summary_row(path, record, mtime_ns)
        if row is None:
            counts["active_or_foreign"] += 1
            continue
        candidates.append((path, record, row))
    counts["settled_records"] = len(candidates)
    if dry_run:
        present = {row["task_id"]: row for row in read_rows(batch_state_dir)[0]}
        counts["would_add"] = sum(row["task_id"] not in present for _, _, row in candidates)
        counts["would_refresh"] = sum(row["task_id"] in present and _stale(present[row["task_id"]], row)
                                      for _, _, row in candidates)
        return dict(sorted(counts.items()))
    with _locked(batch_state_dir) as path:
        present = {row["task_id"]: row for row in read_rows(batch_state_dir)[0]}
        written = []
        for source, record, row in candidates:
            stored = present.get(row["task_id"])
            if _stale(stored, row):
                # Role and the payload can both follow the sidecar, including when the unlocked
                # payload is null. Re-read under the lock before this row replaces a stored one.
                row = summary_row(source, record, row["source_mtime_ns"]) or row
            if _stale(stored, row):
                written.append(row)
        counts["added"] = sum(row["task_id"] not in present for row in written)
        counts["refreshed"] = len(written) - counts["added"]
        counts["already_present"] = len(candidates) - len(written)
        if written:
            # The existing lines, then the new ones last, so a new line supersedes an older row of its task id.
            temporary = path.with_name(f".{ROWS_FILE}.{os.getpid()}.{time.time_ns()}.tmp")
            try:
                with os.fdopen(_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                    try:
                        with os.fdopen(_open(path, os.O_RDONLY), "rb") as existing:
                            tail = b"\n"
                            while chunk := existing.read(1 << 20):
                                stream.write(chunk)
                                tail = chunk[-1:]
                            if tail != b"\n":
                                stream.write(b"\n")
                    except FileNotFoundError:
                        pass
                    for row in written:
                        stream.write(_line(row))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return dict(sorted(counts.items()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.api.model_stats_rows",
        description=(
            "Backfill the durable model-stats summary rows (#581) from every dispatch task record, hot and archived.\n"
            "Rerun after an update to recover review evidence from retained task results. "
            "Not for live statistics: the Monitor route /api/fleet/model-stats reads the rows."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill --dry-run\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill --batch-state-dir batch_state\n\n"
            "Outputs: appends missing rows to <batch-state-dir>/model-stats/rows.jsonl (atomic replace under\n"
            "rows.lock) and prints one JSON object of counts (settled_records, added, refreshed, already_present,\n"
            "unreadable, duplicate_task_ids, active_or_foreign; would_add and would_refresh with --dry-run).\n"
            "A refreshed row replaces one taken from an older version of its record, or one whose review the\n"
            "record's sidecar no longer gives (rows carry nothing over from the stored row). Task records are never\n"
            "changed.\n\n"
            "Exit codes: 0 rows written or nothing to add; 1 the task store or the rows file could not be used.\n\n"
            "Related: lu_machinery/api/model_stats_router.py, docs/runbooks/model-stats.md, issue #581."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="backfill")
    run = commands.add_parser(
        "backfill", help="Add a summary row for every settled task record that has none (idempotent).",
        description="Add a summary row for every settled task record (hot directory, then archive) that has none.",
    )
    run.add_argument(
        "--batch-state-dir", type=Path, default=None,
        help="The batch_state directory holding tasks/ and model-stats/ (default: the dispatch task store's "
             "parent, LU_TASKS_DIR's parent when set). Example: --batch-state-dir batch_state",
    )
    run.add_argument(
        "--dry-run", action="store_true",
        help="Count the rows that would be added or refreshed and write nothing (default: write).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    batch_state_dir = args.batch_state_dir if args.batch_state_dir is not None else tasks_dir().parent
    tasks = batch_state_dir / "tasks"
    if not tasks.is_dir():
        print(json.dumps({"error": "task store not found"}), file=sys.stderr)
        return 1
    try:
        counts = backfill(tasks, batch_state_dir, dry_run=args.dry_run)
    except OSError as exc:
        print(json.dumps({"error": f"rows file unavailable: {type(exc).__name__}"}), file=sys.stderr)
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The shared path/status classifier for recorded review and landing admission."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration.integration_sweep import pr_file_paths

ReviewTier = Literal["low", "medium", "ukrainian-content", "high"]
_RANK: dict[ReviewTier, int] = {"low": 0, "medium": 1, "ukrainian-content": 2, "high": 3}
_MATCH_ORDER: tuple[ReviewTier, ...] = ("high", "ukrainian-content", "low")
_STATUSES = {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}


def classify_pr_files(rows: object, *, table_path: Path | None = None) -> ReviewTier:
    """Classify a complete REST PR-files response; unknown input/table is High.

    Use the sweep's validator, including its rename and 3,000-file protections.
    No patch inspection: comment-only source edits conservatively stay Medium.
    """
    try:
        paths = pr_file_paths(rows)
        if not paths or any(
            path.startswith("/") or "\\" in path or any(part in {".", "..", ""} for part in path.split("/"))
            or PurePosixPath(path).as_posix() != path
            for path in paths
        ):
            return "high"
        table = yaml.safe_load((table_path or machinery_path("config/review_tiers.yaml")).read_text(encoding="utf-8"))
        if (
            not isinstance(table, dict) or set(table) != {"schema_version", "default", "tiers"}
            or type(table["schema_version"]) is not int or table["schema_version"] != 1
            or table["default"] != "medium"
        ):
            return "high"
        tiers = table["tiers"]
        if not isinstance(tiers, dict) or set(tiers) != {"high", "ukrainian-content", "low"}:
            return "high"
        if any(
            not isinstance(patterns, list) or not patterns
            or any(not isinstance(pattern, str) or not pattern for pattern in patterns)
            for patterns in tiers.values()
        ):
            return "high"
        result: ReviewTier = "low"
        # pr_file_paths validated the shape; every status must also be explicit.
        assert isinstance(rows, list)
        for row in rows:
            status = row.get("status")
            if not isinstance(status, str) or status not in _STATUSES:
                return "high"
            for path in pr_file_paths([row]):
                tier: ReviewTier = next(
                    (tier for tier in _MATCH_ORDER
                     if any(fnmatchcase(path, pattern) for pattern in tiers[tier])),
                    "medium",
                )
                if status == "removed" and tier not in {"high", "ukrainian-content"}:
                    tier = "low"
                if _RANK[tier] > _RANK[result]:
                    result = tier
        return result
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError):
        return "high"


def requires_second_review(rows: object, *, table_path: Path | None = None) -> bool:
    """Whether landing needs the binding second review (operator 2026-11-11).

    True for a High-tier PR (``classify_pr_files``) or one that changes a security-sensitive
    path (``security_paths``); the latter lists files the tier table does not. The trigger is
    the changed paths alone: a declared review risk never adds or removes it. Unreadable or
    unclassifiable input requires it.
    """
    from lu_machinery.review.security_paths import is_security_sensitive_change

    try:
        if classify_pr_files(rows, table_path=table_path) == "high":
            return True
        return is_security_sensitive_change(pr_file_paths(rows))
    except Exception:  # unreadable input never waives the second review
        return True


# Only this configured repository ships through a separately released wheel.
CARBON_DEBT_REPOSITORY = "github.com/learn-ukrainian/machinery"
CARBON_DEBT_LABEL = "carbon-debt"


def allows_carbon_debt(repository: str | None) -> bool:
    """Absent Carbon approval may defer to release only in the named repository."""
    from lu_machinery.opsec.prepublish import normalize_repository

    return repository is not None and normalize_repository(repository) == CARBON_DEBT_REPOSITORY

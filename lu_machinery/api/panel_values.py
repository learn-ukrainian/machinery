"""Small fail-closed scalar readers for the public machinery panels.

Each owning router selects its fields. No diagnostic text, arbitrary mapping
keys or provider labels are admitted by a text-shape heuristic.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any, overload

from lu_machinery.review.model_catalog import ModelCatalogError, canonical_model_id, load_model_catalog

AGENTS = frozenset({"claude", "codex", "gemini", "agy", "grok", "cursor", "muse", "opencode", "openrouter", "deepseek"})


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def rows(value: object, limit: int) -> list[dict[str, Any]]:
    return [item for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


@overload
def choice(value: object, allowed: Collection[str], default: str) -> str: ...


@overload
def choice(value: object, allowed: Collection[str], default: None = None) -> str | None: ...


def choice(value: object, allowed: Collection[str], default: str | None = None) -> str | None:
    return value if isinstance(value, str) and value in allowed else default


def reason(value: object, allowed: Collection[str]) -> str | None:
    return None if value is None else choice(value, allowed, "provider_error")


def number(value: object) -> int | float | None:
    # Bound JSON magnitudes as well as length; bool is not a measured number.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -1e15 <= value <= 1e15 or not math.isfinite(value):
        return None
    return value


def timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 40:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def scalars(
    row: dict[str, Any], *, numbers: Collection[str] = (),
    flags: Collection[str] = (), times: Collection[str] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {key: number(row[key]) for key in numbers if key in row}
    result.update({key: row[key] if type(row[key]) is bool else None for key in flags if key in row})
    result.update({key: timestamp(row[key]) for key in times if key in row})
    return result


def observation_age_s(value: object) -> float | None:
    """Age of the observation, rather than of its HTTP response or cache insert."""
    stamp = timestamp(value)
    if stamp is None:
        return None
    observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        return None
    return max(0.0, (datetime.now(UTC) - observed).total_seconds())


def snapshot_headers(
    age_s: float | None, stale_after_s: float, *, unavailable: bool = False, stale: bool = False,
) -> dict[str, str]:
    """A common freshness contract for the machinery panels, including failed sources."""
    age = number(age_s)
    state = "unavailable" if unavailable or age is None else "stale" if stale or age > stale_after_s else "fresh"
    return {
        "Cache-Control": "no-store",
        "X-Snapshot-Age-S": "unknown" if age is None else f"{max(0.0, age):.3f}",
        "X-Snapshot-Stale-After-S": str(stale_after_s),
        "X-Snapshot-State": state,
    }


# Public panel identifiers. Dots are refused: a filename-safe token can still be
# untrusted text. Length 64 matches dispatcher ids in use; no other shape is admitted.
_TASK_REFERENCE_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_TASK_REFERENCE_MAX_LEN = 64


def task_reference(value: object) -> str | None:
    """Admit a short lowercase token; withhold every other value."""
    if not isinstance(value, str) or not 0 < len(value) <= _TASK_REFERENCE_MAX_LEN:
        return None
    if any(character not in _TASK_REFERENCE_ALPHABET for character in value):
        return None
    return value


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    return load_model_catalog()


def model(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 100:
        return None
    try:
        catalog = _catalog()
        resolved = canonical_model_id(value, catalog)
        return resolved if resolved in catalog["models"] else None
    except (ModelCatalogError, OSError, ValueError):
        return None

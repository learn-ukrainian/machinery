"""Machinery configuration for the Monitor API: roots, stores and the product entry.

Curriculum configuration (levels, seminar tracks, textbook roots) is product
code in ``scripts.project_api.config`` (Monitor split, C-4687).
"""

import os
from pathlib import Path

from lu_machinery.common.bridge_paths import configured_bridge_db_path
from lu_machinery.common.repo_root import product_root, state_root


def _live_repo_root() -> Path:
    """Return the checkout Git operations and child tools must target."""
    configured = os.environ.get("LEARN_UK_REPO_ROOT")
    if not configured:
        return product_root()
    supplied = Path(configured)
    if not supplied.is_absolute():
        raise RuntimeError("LEARN_UK_REPO_ROOT must be an absolute path")
    candidate = supplied.resolve()
    if not candidate.is_dir() or not (candidate / ".git").exists():
        raise RuntimeError(f"LEARN_UK_REPO_ROOT is not a Git checkout: {candidate}")
    return candidate


def __getattr__(name: str) -> Path:
    """Resolve API paths on first use, without requiring roots at import.

    Product content belongs to the selected snapshot. Git operations and mutable
    stores belong to the configured live checkout and shared state respectively.
    """
    if name in {"PROJECT_ROOT", "PRODUCT_ROOT"}:
        selected = globals().get("PRODUCT_ROOT") or product_root()
        globals()["PROJECT_ROOT"] = globals()["PRODUCT_ROOT"] = selected
    elif name == "LIVE_REPO_ROOT":
        selected = _live_repo_root()
    elif name == "CURRICULUM_ROOT":
        selected = __getattr__("PRODUCT_ROOT") / "curriculum" / "l2-uk-en"
    elif name == "DASHBOARDS_DIR":
        selected = __getattr__("PRODUCT_ROOT") / "dashboards"
    elif name == "MESSAGE_DB":
        selected = configured_bridge_db_path(state_root())
    elif name == "BATCH_STATE_DIR":
        override = os.environ.get("LEARN_UKRAINIAN_BATCH_STATE_DIR")
        selected = Path(override) if override is not None else state_root() / "batch_state"
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = selected
    return selected


# Server settings
API_HOST = "127.0.0.1"  # nosec B104 — bind to localhost only
API_PORT = 8765

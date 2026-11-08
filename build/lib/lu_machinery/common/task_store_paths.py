"""Call-time location of the shared dispatch task store."""

from __future__ import annotations

import os
from pathlib import Path

from lu_machinery.common.repo_root import state_root


def tasks_dir() -> Path:
    """Use a test override or the primary checkout's dispatch task store."""
    override = os.environ.get("LU_TASKS_DIR")
    if override:
        return Path(override)
    return state_root() / "batch_state" / "tasks"

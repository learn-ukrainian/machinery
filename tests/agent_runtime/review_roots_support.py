"""Helpers copied from learn-ukrainian ``tests/test_review_isolation.py`` at 5aba2adb.

The source file is product-kept or owned by another tranche; its tests do not move here.
Only the helpers that tests in this directory import are copied. Their bodies are verbatim; the import block keeps what they use, with machinery import paths.
"""

from __future__ import annotations

import secrets
from pathlib import Path


def _private_review_roots(tmp_path: Path, label: str = "review") -> tuple[Path, Path]:
    """Create the parent-owned write/exec layout required by production."""
    write = tmp_path / f"{label}-write"
    execution = tmp_path / f"{label}-exec"
    write.mkdir(mode=0o700)
    execution.mkdir(mode=0o700)
    for child in ("tmp", "home", "exec"):
        (write / child).mkdir(mode=0o700)
    (write / "empty-mcp.json").write_text('{"mcpServers":{}}\n', encoding="utf-8")
    (write / "empty-mcp.json").chmod(0o400)
    # Exec-root validation requires the review temp sentinel.
    marker = execution / ".lu-review-root"
    marker.write_text(f"lu-review-root-v1:{secrets.token_hex(32)}\n", encoding="ascii")
    marker.chmod(0o400)
    return write, execution

"""Helpers copied from learn-ukrainian ``tests/agent_runtime/adapters/test_agy_review_permissions.py`` at 5aba2adb.

The source file is product-kept or owned by another tranche; its tests do not move here.
Only the helpers that tests in this directory import are copied. Their bodies are verbatim; the import block keeps what they use, with machinery import paths.
"""

from __future__ import annotations


def auto_denial(kind, target="<target>"):
    # Verbatim CLI line retained in the three named tasks' stderr.log files;
    # the logs had command, command and mcp respectively, with no real target.
    return (
        f'jetski: no output produced — a tool required the "{kind}" permission that headless mode cannot prompt for, '
        f"so it was auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. {kind}({target})). "
        "Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools."
    )


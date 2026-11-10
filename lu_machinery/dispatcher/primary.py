"""Run the dispatcher code of the primary checkout from any linked worktree."""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from lu_machinery.common.repo_root import in_tree_source, machinery_path, main_checkout_root, primary_root

ENTRY = Path(Path(__file__).absolute().parents[1].name, "dispatch.py")


def _refuse(message: str) -> NoReturn:
    print(f"dispatcher: {message}", file=sys.stderr)
    raise SystemExit(2)


def exec_primary(
    argv: list[str], *, entry: Path = ENTRY, refuse: Callable[[str], NoReturn] = _refuse
) -> None:
    """Replace this process with a primary-checkout entry point from a linked worktree.

    A worktree on an older branch would otherwise supervise tasks with stale dispatcher code.
    The default entry is the dispatcher; other gate CLIs can share this bootstrap.
    The caller's working directory and arguments are kept, so relative paths resolve as before.
    Returns in the primary checkout and in a checkout without Git metadata; exits 2 when a
    linked worktree's primary cannot be resolved. A caller may supply a non-returning
    refusal handler to format the reason itself.

    The primary here is the running code's own primary, read from its Git metadata:
    the configured target primary (``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``) never
    chooses which dispatcher code runs. The re-exec uses this process's interpreter under
    ``-I``: the entry puts the primary root on ``sys.path`` itself, so neither the entry's
    directory nor ``PYTHONPATH`` can shadow a module.
    Installed code validates its configured primary and stays in this process; help
    needs no operational root.
    """
    if not in_tree_source():
        # Installed code stays installed; a product checkout cannot select its replacement.
        if not any(arg in {"-h", "--help"} for arg in argv):
            primary_root()
        return
    root = machinery_path("dispatch.py").parent.parent
    try:
        primary = main_checkout_root(root).resolve()
    except ValueError as exc:  # undecodable metadata, or a NUL in its gitdir target
        refuse(f"cannot resolve the primary checkout of linked worktree {root}: {exc}")
    if primary == root:
        if (root / ".git").is_file():
            refuse(f"cannot resolve the primary checkout of linked worktree {root}")
        return
    entry = primary / entry
    if not entry.is_file():
        refuse(f"primary checkout has no entry point: {entry}")
    os.execv(sys.executable, [sys.executable, "-I", str(entry), *argv])

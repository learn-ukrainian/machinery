"""Provider registry. Each provider module exposes:

build(spec: TaskSpec, env: dict[str, str]) -> Invocation
parse(stdout: str, stderr: str, returncode: int, final_text: str | None) -> ParsedResult
"""

from __future__ import annotations

from collections.abc import Mapping
from types import ModuleType

PROVIDERS = ("codex", "claude", "cursor", "agy", "muse", "opencode")

# Read-only must be unable to edit. The supervisor confines native routes in write_boundary's
# read-only mounts, except Claude, which retains its existing per-shell boundary pending enrollment.
# AGY retains its own read-only bubblewrap boundary (agy_sandbox, C-3027).
# Provider permissions are additional restrictions, never a substitute for the OS boundary.
READ_ONLY_REFUSALS: dict[str, str] = {}


def read_only_refusal(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """The pre-launch provider refusal for read-only, or None; the supervisor still enforces OS containment.

    AGY is refused where its bubblewrap boundary cannot start. With the run's environment, Claude is
    also refused while any managed policy could widen its lock.
    """
    reason = READ_ONLY_REFUSALS.get(name)
    if reason is None and name == "agy":
        from lu_machinery.dispatcher import agy_sandbox

        reason = agy_sandbox.unavailable()
    if reason is None and name == "claude" and env is not None:
        from lu_machinery.dispatcher.providers import claude

        reason = claude.managed_policy(env)
    if reason is None:
        return None
    usable = ", ".join(provider for provider in PROVIDERS if provider not in READ_ONLY_REFUSALS and provider != name)
    return f"read_only_unenforceable: {name} cannot run --mode read-only: {reason}; use {usable}"


def load(name: str) -> ModuleType:
    if name == "codex":
        from lu_machinery.dispatcher.providers import codex as module
    elif name == "claude":
        from lu_machinery.dispatcher.providers import claude as module
    elif name == "cursor":
        from lu_machinery.dispatcher.providers import cursor as module
    elif name == "agy":
        from lu_machinery.dispatcher.providers import agy as module
    elif name == "muse":
        from lu_machinery.dispatcher.providers import muse as module
    elif name == "opencode":
        from lu_machinery.dispatcher.providers import opencode as module
    else:
        raise ValueError(f"unknown agent {name!r}; known: {', '.join(PROVIDERS)}")
    return module

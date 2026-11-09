"""Guard: the bridge package must load under a single module identity (#6812).

The canonical identity is ``lu_machinery.ai_agent_bridge``. The bare ``ai_agent_bridge`` identity
used to load as a second ``sys.modules`` entry with independent module-level
state, so ``monkeypatch`` on one identity never affected the other.

These tests fail if any test file reintroduces a non-canonical bridge import
or quoted module target, or if the bare identity is loaded at runtime.

The installed ``lu_machinery.api`` package is scanned too: the Monitor API boots inside the test process
(FastAPI lifespan preload, comms sweep background thread), and bare-identity
imports there were the runtime loaders that made this guard flaky under
pytest-xdist (#6838 red on an unrelated CSS change).
"""

from __future__ import annotations

import re
from pathlib import Path

import lu_machinery.api
import pytest

pytestmark = [pytest.mark.repo_invariant, pytest.mark.repo_wide]

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve()

# Match the bare identity as a module path segment, not a substring (#M-16):
# ``from ai_agent_bridge`` / ``import ai_agent_bridge._db`` are non-canonical,
# while ``scripts.ai_agent_bridge`` (the canonical form) never matches because
# the segment must directly follow ``from``/``import`` or an opening quote.
_NONCANONICAL_PATTERNS = {
    "import statement": re.compile(r"^\s*(?:from|import)\s+ai_agent_bridge(?:[\s.]|$)", re.M),
    "quoted module target": re.compile(r"""['"]ai_agent_bridge\."""),
}

# Static scan roots: the test suite plus the in-process API package, as
# installed. The rest of the package deliberately keeps blessed bare fallbacks
# for direct-script production runs (``__main__`` script mode, agent_runtime
# adapters), which a blanket scan would flag; the API package never runs that way.
_API_ROOT = Path(lu_machinery.api.__file__).resolve().parent
_SCAN_ROOTS = (_TESTS_ROOT, _API_ROOT)


def _iter_scanned_files():
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() != _SELF:
                yield path


def test_no_noncanonical_bridge_imports_in_test_suite() -> None:
    """No scanned file may reference the bare ``ai_agent_bridge`` identity."""
    violations: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8")
        for kind, pattern in _NONCANONICAL_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path}:{line}: {kind}: {match.group()!r}")
    assert not violations, (
        "Non-canonical `ai_agent_bridge` module identity found; use `lu_machinery.ai_agent_bridge` "
        "everywhere (#6812):\n" + "\n".join(violations)
    )

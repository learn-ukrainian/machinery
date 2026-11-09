"""Opt-in roots for the agent-runtime tests: the shared product tree plus two documented stand-ins.

``configured_roots`` (``tests/helpers/product_tree.py``) selects the hermetic product tree and a git
primary repository. Two product modules the machinery imports from the product root are not byte
copyable, because the real ones import a dozen product modules this repository does not hold:
``scripts/curriculum/review_attempt_hooks.py`` (named by ``review_hooks.v1.json``) and the receipt
ledger it returns. ``fixtures/product_stand_ins`` holds a minimal stand-in of each: the ledger keeps
the review tool contract (the tool names are the product's) and its error type, and nothing else.

``agent_runtime_roots`` copies the shared tree, overlays the stand-ins and selects the copy, so the
shared tree stays read-only for every other test. ``load_configured_module`` leaves the ``scripts``
modules it loads in ``sys.modules`` and refuses a later load from another root, so the fixture
removes them before the test and puts back what was there before, after it. It also clears the
review-hooks cache, which holds a module whose lazy ``scripts`` imports would otherwise find
nothing. A module opts in with::

    from tests.agent_runtime.product_roots import agent_runtime_roots  # noqa: F401

    pytestmark = pytest.mark.usefixtures("agent_runtime_roots")
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.review import product_hooks
from tests.helpers.product_tree import configured_roots  # noqa: F401 - re-exported for the opt-in modules

_STAND_INS = Path(__file__).resolve().parent / "fixtures" / "product_stand_ins"


def _product_modules() -> dict[str, object]:
    return {name: module for name, module in sys.modules.items() if name == "scripts" or name.startswith("scripts.")}


@pytest.fixture
def agent_runtime_roots(
    configured_roots: SimpleNamespace,  # noqa: F811 - the fixture, requested by name
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SimpleNamespace]:
    """Select the shared product tree with the stand-in modules added; yield ``product`` and ``primary``."""
    product = tmp_path_factory.getbasetemp() / "agent-runtime-product-tree"
    if not product.exists():
        shutil.copytree(configured_roots.product, product)
        shutil.copytree(_STAND_INS, product, dirs_exist_ok=True)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    before = _product_modules()
    for name in before:
        del sys.modules[name]
    product_hooks._attempt_hooks_from.cache_clear()
    try:
        yield SimpleNamespace(product=product, primary=configured_roots.primary)
    finally:
        product_hooks._attempt_hooks_from.cache_clear()
        for name in _product_modules():
            del sys.modules[name]
        sys.modules.update(before)

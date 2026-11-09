"""One opt-in fixture: a hermetic product tree and a primary checkout in tmp, as configured roots.

The learn-ukrainian tests ran inside the product checkout, where the adapters found the
product's own files by checkout layout. The installed package derives no product root
(``repo_root.product_root`` needs ``LU_PRODUCT_ROOT``) and no primary checkout
(``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``), and machinery holds none of the product files
that its adapters read through those roots. ``tests/fixtures/product_tree/`` holds byte
copies of the product files that the ported tests read, from learn-ukrainian at commit
5aba2adb96e13b3f4a5dc2abbe5ed71b7fb81d00 (the frozen source of the port):

- ``agents_extensions/shared/settings.json``: the Claude worker hook list that
  ``ClaudeAdapter`` turns into guard settings.
- ``.mcp/servers/sources/server.py``: the Sources server whose tool declarations
  ``sources_read_only.sources_tool_sets`` partitions.
- ``docs/review-protocol.md``: the review protocol the bridge prepends to review prompts.
- ``.github/workflows/ci.yml``, ``scripts/curriculum/deploy_paths.v1.json``,
  ``scripts/curriculum/admission_policy.v1.json``, ``scripts/curriculum/review_hooks.v1.json``,
  ``agents_extensions/agy/hooks.json``, ``agents_extensions/shared/rules/core-curriculum.md``
  and ``.nvmrc``: the workflow policy, curriculum policy data, hook lists, rules text and
  Node version that the publisher, admission and review code read.

A test opts in, by fixture argument or by ``pytestmark``::

    from tests.helpers.product_tree import configured_roots  # noqa: F401

    pytestmark = pytest.mark.usefixtures("configured_roots")

Nothing is global: a test that checks the no-root refusal does not opt in and still sees
no root. The fixture sets both variables with ``monkeypatch``, so each is restored after the
test, and it never points at the checkout that runs the tests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "product_tree"


@pytest.fixture
def configured_roots(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Select the product tree and a fresh git primary repository; return both as ``product`` and ``primary``.

    The product tree is built once per session, under the session's base directory, and only read.
    """
    product = tmp_path_factory.getbasetemp() / "product-tree"
    if not product.exists():
        shutil.copytree(_FIXTURE, product)
    primary = tmp_path_factory.mktemp("primary")
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(
            [*git, *args], cwd=primary, check=True, capture_output=True, timeout=30
        )
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(primary))
    return SimpleNamespace(product=product, primary=primary)

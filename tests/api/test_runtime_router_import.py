"""Planted top-level ``agent_runtime`` must stay unbound when the API router loads.

Defect class: ``runtime_router`` inserts its parent directory on ``sys.path`` and
imports ``agent_runtime`` as a bare top-level package. An installed API consumer
then collides with any other ``agent_runtime`` already on ``sys.path``. Reverting
that shim binds ``agent_runtime`` in ``sys.modules`` and fails this test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lu_machinery.common.repo_root import machinery_root

_CHILD = r"""
import sys
from pathlib import Path

planted_root, repo = sys.argv[1:]
repo_path = Path(repo)
sys.path = [entry for entry in sys.path if entry not in {"", str(Path.cwd())}]
sys.path.insert(0, str(repo_path))
sys.path.insert(0, planted_root)

import lu_machinery.api.runtime_router  # noqa: F401

bare = [
    name
    for name in sys.modules
    if name == "agent_runtime" or name.startswith("agent_runtime.")
]
if bare:
    raise SystemExit("bare agent_runtime imported: " + ", ".join(sorted(bare)))
loaded = Path(planted_root) / "agent_runtime" / "LOADED"
if loaded.exists():
    raise SystemExit("planted agent_runtime executed")
registry = sys.modules.get("lu_machinery.agent_runtime.registry")
registry_file = getattr(registry, "__file__", None)
expected = repo_path / "lu_machinery"
if registry is None or registry_file is None or not Path(registry_file).is_relative_to(expected):
    raise SystemExit(f"registry did not come from the running release: {registry_file}")
"""


def test_planted_top_level_agent_runtime_is_not_imported_by_runtime_router(tmp_path: Path) -> None:
    planted_root = tmp_path / "planted"
    package = planted_root / "agent_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('LOADED').write_text('planted\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (package / "registry.py").write_text("AGENTS = {}\n", encoding="utf-8")
    repo = machinery_root().parent
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD, str(planted_root), str(repo)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

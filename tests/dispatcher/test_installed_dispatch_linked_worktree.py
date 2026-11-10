"""The installed dispatcher runs from a linked product worktree without a product re-exec (machinery #100).

``exec_primary`` replaces an in-tree dispatcher with its primary checkout's entry point;
installed code stays in its own process (``lu_machinery/dispatcher/primary.py``). This
runs the release wheel, installed alone into a fresh virtual environment, with the drill's
read-only dry-run command from a linked worktree of a product checkout. That checkout holds
a decoy ``lu_machinery`` package whose ``dispatch.py`` entry would mark and exit if anything
executed or imported it. Import tracing (``PYTHONVERBOSE``) names every module file loaded.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from tests.helpers.wheel import install_release, release_wheel

REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCT_TREE = REPO_ROOT / "tests" / "fixtures" / "product_tree"
_DECOY = 'import pathlib, sys\npathlib.Path(__file__).with_suffix(".ran").touch()\nsys.exit(97)\n'
_CODE_OBJECT = re.compile(r"^# code object from '(?P<path>[^']+)'$", re.MULTILINE)
# The entry, the re-exec decision, the command line, repository resolution and the root rule.
_EXECUTED = ("dispatch", "dispatcher/primary", "dispatcher/cli", "dispatcher/git", "common/repo_root")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def test_installed_dispatch_from_linked_product_worktree_stays_installed(tmp_path):
    python = install_release(release_wheel(REPO_ROOT, tmp_path), tmp_path / "venv", dependencies=True)
    site = (
        Path(
            subprocess.run(
                [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        / "lu_machinery"
    )

    primary = tmp_path / "product"
    shutil.copytree(_PRODUCT_TREE, primary)
    decoy = primary / "lu_machinery"
    decoy.mkdir()
    (decoy / "__init__.py").write_text(_DECOY, encoding="utf-8")
    (decoy / "dispatch.py").write_text(_DECOY, encoding="utf-8")
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "product")
    # The dry-run plans from origin/main, as in a product clone.
    _git(primary, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = _git(primary, "rev-parse", "HEAD")
    linked = tmp_path / "product-linked"
    _git(primary, "worktree", "add", "-q", "-b", "feature", str(linked))
    assert (linked / ".git").is_file()
    assert (linked / "lu_machinery" / "dispatch.py").is_file()

    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "PYTHONVERBOSE": "1",
        "LEARN_UKRAINIAN_PYTHON": str(python),
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(primary),
        "LU_PRODUCT_ROOT": str(linked),
    }
    result = subprocess.run(
        [
            str(python),
            "-P",
            "-m",
            "lu_machinery.dispatch",
            "dispatch",
            "--agent",
            "sol",
            "--model",
            "gpt-6.1-sol",
            "--effort",
            "high",
            "--mode",
            "read-only",
            "--repo",
            "public",
            "--worktree",
            "--prompt",
            "Report repository identity and resolved machinery version; change nothing.",
            "--dry-run",
        ],
        cwd=linked,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    plan = json.loads(result.stdout)
    # The linked worktree's product resolves to its primary checkout, at the planned start.
    assert Path(plan["worktree"]).parents[1] == primary / ".worktrees" / "dispatch"
    assert Path(plan["record_path"]).parent == primary / "batch_state" / "tasks"
    assert plan["start_sha"] == head
    # The dry-run creates nothing.
    assert not (primary / ".worktrees").exists()
    assert not (primary / "batch_state").exists()
    assert not any(home.iterdir())
    assert _git(primary, "status", "--porcelain") == ""
    assert _git(linked, "status", "--porcelain") == ""
    # No checkout copy ran: not by re-exec, not by import.
    assert not list(decoy.glob("*.ran")) and not list((linked / "lu_machinery").glob("*.ran"))
    sources = {_source(Path(match["path"])) for match in _CODE_OBJECT.finditer(result.stderr)}
    machinery = {path for path in sources if "lu_machinery" in path.parts}
    outside = sorted(str(path) for path in machinery if not path.is_relative_to(site))
    assert outside == [], outside
    expected = {site / f"{module}.py" for module in _EXECUTED}
    assert expected <= machinery, sorted(map(str, expected - machinery))
    checkouts = (primary, linked, REPO_ROOT)
    assert not [str(path) for path in sources if any(path.is_relative_to(root) for root in checkouts)]


def _source(code: Path) -> Path:
    """The module file behind a traced code object: ``pkg/__pycache__/x.cpython-312.pyc`` is ``pkg/x.py``."""
    if code.parent.name == "__pycache__":
        return code.parent.parent / f"{code.name.split('.', 1)[0]}.py"
    return code

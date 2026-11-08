"""Installed-wheel import census (Refs #100, R46 and R58).

Defect class: an unprefixed first-party import in the installed wheel.
A consumer pytest ``pythonpath = ["scripts"]`` then binds that name to the
consumer's in-tree copy instead of this release. Reverting any prefixed
import or string-built module name in ``lu_machinery/`` makes this fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path

from tests.helpers.wheel import release_wheel

REPO = Path(__file__).resolve().parents[2]

# Standalone census. The clean interpreter runs this with python -I; it does
# not import the checkout or this test module.
_CENSUS = r"""
import ast, json, sys
from pathlib import Path

package = Path(sys.argv[1])
stdlib = sys.stdlib_module_names
import_funcs = {"import_module", "__import__", "import_named_module", "find_spec"}

def is_mod(parts):
    if not parts or any(not part.isidentifier() for part in parts):
        return False
    base = package.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return True
    return base.is_dir() and any(child.suffix == ".py" for child in base.glob("*.py"))

def resolves(mod, file):
    if not mod or mod.startswith("lu_machinery"):
        return False
    parts = tuple(mod.split("."))
    if parts[0] in stdlib:
        return False
    if is_mod(parts):
        return True
    sibling = file.parent.relative_to(package).parts + parts
    return is_mod(sibling)

hits = []
for path in package.rglob("*.py"):
    if "__pycache__" in path.parts:
        continue
    rel = path.relative_to(package).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if resolves(node.module, path):
                hits.append({"path": rel, "line": node.lineno, "module": node.module})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if resolves(alias.name, path):
                    hits.append({"path": rel, "line": node.lineno, "module": alias.name})
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in import_funcs or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "." in arg.value:
                if resolves(arg.value, path):
                    hits.append({"path": rel, "line": arg.lineno, "module": arg.value})
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or not node.values:
            continue
        first = node.values[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        prefix = first.value.rstrip(".")
        if "." not in prefix or first.value.startswith("lu_machinery"):
            continue
        if resolves(prefix, path):
            hits.append({"path": rel, "line": first.lineno, "module": first.value})
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not any("MODULE" in name for name in names):
            continue
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and "." in elt.value and resolves(elt.value, path):
                hits.append({"path": rel, "line": elt.lineno, "module": elt.value})
print(json.dumps(hits))
"""


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


def test_installed_wheel_has_no_unprefixed_first_party_imports(tmp_path: Path) -> None:
    """Defect class: a bare first-party import shipped in the wheel."""
    wheel = release_wheel(REPO, tmp_path)
    consumer = tmp_path / "consumer"
    venv.EnvBuilder(with_pip=True).create(consumer)
    executable = consumer / "bin" / "python"
    installed = _run(
        [str(executable), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", str(wheel)],
        cwd=tmp_path,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    site = next((consumer / "lib").glob("python*/site-packages/lu_machinery"))
    census = _run(
        [str(executable), "-I", "-c", _CENSUS, str(site)],
        cwd=scratch,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert census.returncode == 0, census.stdout + census.stderr
    hits = json.loads(census.stdout)
    assert hits == [], json.dumps(hits, indent=2)

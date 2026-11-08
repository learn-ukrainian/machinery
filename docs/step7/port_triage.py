"""PORT-1108 step 1: conservative subject triage of the census ``port-first`` paths.

One-shot, reproducible documentation tool (not a runtime module, no tests of its
own). It reads only Git objects at frozen SHAs: learn-ukrainian (LU) at
``LU_SHA`` (the census base) and machinery at ``MACHINERY_SHA``. Working trees
are never read.

For every ``disposition: port-first`` entry of
``docs/step7/test-ownership-census.json`` it decides ``subject_status``:

- ``SUBJECT-GONE``: every subject the file exercises is a machinery path that is
  absent from machinery at ``MACHINERY_SHA``, with no renamed or moved
  implementation, no product-retained copy and no live caller.
- ``SUBJECT-GONE-PROTECTED``: SUBJECT-GONE, but the file falls in a protected
  test-quality class (LU ``docs/best-practices/test-quality.md:13-18,49-75``);
  separate review before any deletion.
- ``SUBJECT-PRESENT``: anything else, including an empty subject set and any
  unresolved dynamic target.

Data and fixture paths take the status of their users; a support module or
conftest is gone only if its own subjects are gone and every user is gone.

Evidence used per Python file (transitively through imported ``tests`` helper
modules, ``pytest_plugins`` and the conftest fixtures it requests):

- imports and aliases (qualified, relative, unqualified through
  ``pythonpath = ["scripts"]``), including function-local imports;
- literal dynamic imports (``importlib.import_module``, ``__import__``,
  ``import_module``, ``pytest.importorskip``, ``runpy.run_module``);
- dotted names in non-docstring strings (``mock.patch`` / ``monkeypatch``
  targets, ``-m`` modules);
- tracked repository paths in strings, ``/`` path chains and
  ``os.path.join`` calls (script targets and checkout-file reads).

Unresolved targets (a non-literal dynamic import or patch target, an f-string
module or path) are recorded and force SUBJECT-PRESENT.

A machinery subject is resolved through the rename map
(``lu_machinery/lint/rename-map.json`` at ``MACHINERY_SHA``; LU ``scripts/X``
becomes ``lu_machinery/X``, other paths keep their name), both tracked-file
inventories, and machinery history from the extraction snapshot
(``SNAPSHOT_SHA``) to ``MACHINERY_SHA``: Git rename detection
(``git log -M --diff-filter=R``, then ``--follow`` on the result), same-commit
low-similarity pairing for each deletion (``-M20%``), and a search for every
imported attribute defined anywhere in machinery.

Usage (from any directory)::

    python3 -I docs/step7/port_triage.py --lu-repo <LU checkout> \\
        --machinery-repo <machinery checkout> [--write <census.json>] [--report <report.json>]

Without ``--write`` it prints the counts only.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

LU_SHA = "5aba2adb96e13b3f4a5dc2abbe5ed71b7fb81d00"
MACHINERY_SHA = "990d71ecd89b44a035123e55e49c9c438fda97e5"
SNAPSHOT_SHA = "e91a51a939a248250108ed536edca43e8d68c1e1"  # machinery root: the extraction snapshot
CENSUS = "docs/step7/test-ownership-census.json"
RENAME_MAP = "lu_machinery/lint/rename-map.json"

GONE = "SUBJECT-GONE"
GONE_PROTECTED = "SUBJECT-GONE-PROTECTED"
PRESENT = "SUBJECT-PRESENT"

# Why machinery deleted a path, from the deleting commit's message (read with
# ``git log -1 --format=%B <sha>``). Only ``retired`` can make a subject gone.
# ``product-retained``: LU keeps its copy, so the subject lives on in the product.
# ``moved``: the content moved (forwarder removed, shim to a kept module).
# A deletion whose commit is not listed here is unresolved and keeps the file.
DELETION_REASONS: dict[str, tuple[str, str]] = {
    "251d1b9": ("retired", "#141 removes the retired Kimi execution stack"),
    "4b332ce": ("retired", "#62 removes five unreferenced modules (C-4831)"),
    "bdfb837": ("retired", "#71 deletes the retired Kimi canary lane"),
    "58f172f": ("retired", "#65 removes the self-answered rollover quiz (thread_handoff_canary)"),
    "aaba253": (
        "product-retained",
        "#108 keeps product reporting and deployment tools in product; LU retains its copies",
    ),
    "334d51d": ("retired", "#127 R29 deletes agent_watcher; R20 orphan data-volume drop-ins (LU keeps its own copies)"),
    "dca1f10": ("retired", "#3 ports LU #12859, which deleted these tools in LU too"),
    "e4e1b5e": ("moved", "#66 removes the V4 forwarding modules; the product keeps its own implementation"),
    "41198e8": ("moved", "#91 moves the issue-report units into the package"),
}
# Per-path overrides of the commit reason, where one commit mixed kinds.
DELETION_PATH_REASONS: dict[str, tuple[str, str]] = {
    "lu_machinery/check_issue_task_quality.py": (
        "moved",
        "#71 deletes the compatibility shim; the implementation is lu_machinery/ci/check_issue_task_quality.py",
    ),
}
for _dropin in (
    "api",
    "context-guard",
    "data-tier",
    "git-maintenance",
    "issue-quality-report",
    "merge-queue-keeper",
    "opencode-retention",
    "preserved-retention",
    "project-state-reporter",
    "quota-history",
    "reconcile",
    "tmp-sweep",
    "work",
    "worktree-gc",
):
    DELETION_PATH_REASONS[f"packaging/systemd/dropins/learn-ukrainian-{_dropin}.service.d/data-volume.conf"] = (
        "product-retained",
        "#127 R20: learn-ukrainian keeps its own copies",
    )

# Attribute names too generic to prove a moved implementation.
GENERIC_ATTRS = {"main", "run", "cli", "app", "parser", "build_parser", "logger", "log"}

# Paths whose references are inventories of the move itself, not callers.
INVENTORY_PATHS = (
    "lu_machinery/lint/rename-map.json",
    "lu_machinery/lint/mypy_baseline.json",
    "lu_machinery/ci/pytest-file-durations.json",
    ".github/workflows/semgrep-baseline.json",
    "docs/step7/",
    "scripts/lint/mypy_baseline.json",
    "scripts/ci/pytest-file-durations.json",
    "scripts/ci/semgrep-baseline.json",
    "packaging/machinery-move/",
)

# Hand review (file read at LU_SHA): string subjects that are fixture data, not
# reads or targets. Only these exact subjects are dropped, each with its reason.
HAND_REVIEW: dict[str, dict[str, str]] = {
    "tests/test_codex_report.py": {
        "scripts/ai_agent_bridge/_worktree_brief.py": (
            "report-text fixture: the FILES_TOUCHED value inside the parsed CODEX REPORT block (line 24) and "
            "the expected parse_codex_report result (line 42); the file is never read, imported or run"
        ),
    },
}

# Protected test-quality classes (LU test-quality.md K1-K13) for SUBJECT-GONE
# files, decided by reading each file at LU_SHA. Keyed by LU test path.
PROTECTED: dict[str, str] = {
    "tests/test_codex_report.py": "K6 external-format parsing: the CODEX REPORT block parser and validator",
    "tests/test_kimi_coding_oauth.py": "K1 credentials: OAuth token refresh, write-back and failure exit codes",
    "tests/test_kimi_oauth_canary.py": "K1 credentials: refresh-chain canary, token redaction and alerting",
    "tests/test_kimi_oauth_tty_guard.py": "K1 credentials: live-token TTY refusal, 0600 delivery, redaction by token shape",
    "tests/test_kimi_lane_bootstrap.py": "K1 write guard: the CLI rejects an unsafe epic name before writing",
    "tests/test_venv_lock_drift.py": "K2 audit (dependency drift), with fail-closed parsing",
}


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run read-only ``git -C <repo> <args>``: fixed executable, argv list, no shell."""

    argv = ["git", "-C", str(repo), *args]
    # Trusted: the executable is fixed and every argument is a frozen SHA, a tracked path or a literal.
    return subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        argv, capture_output=True, check=False
    )


def git(repo: Path, *args: str) -> str:
    proc = _git_run(repo, *args)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode()


class Blobs:
    """``git cat-file --batch`` reader of ``<sha>:<path>`` blobs."""

    def __init__(self, repo: Path, sha: str) -> None:
        self.sha = sha
        argv = ["git", "-C", str(repo), "cat-file", "--batch"]
        self.proc = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self.cache: dict[str, bytes | None] = {}

    def read(self, path: str) -> bytes | None:
        if path in self.cache:
            return self.cache[path]
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(f"{self.sha}:{path}\n".encode())
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) < 3 or header[1] != b"blob":
            self.cache[path] = None
            return None
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)
        self.cache[path] = data
        return data

    def text(self, path: str) -> str | None:
        data = self.read(path)
        return None if data is None else data.decode("utf-8", "replace")


def machinery_path(lu_path: str) -> str:
    return "lu_machinery/" + lu_path[len("scripts/") :] if lu_path.startswith("scripts/") else lu_path


@dataclass
class Facts:
    """Raw references of one Python file before resolution."""

    modules: set[str] = field(default_factory=set)  # absolute dotted names (imports, dynamic, strings)
    attrs: set[tuple[str, str]] = field(default_factory=set)  # (module, imported attribute)
    weak_modules: set[str] = field(default_factory=set)  # dotted strings that may be modules
    paths: set[str] = field(default_factory=set)  # repository-relative path candidates
    chain_paths: set[str] = field(default_factory=set)  # checkout-anchored path chains (may name a top directory)
    sys_path_roots: set[str] = field(default_factory=set)  # checkout dirs put on sys.path (unqualified imports)
    helpers: set[str] = field(default_factory=set)  # tests.* modules imported
    unresolved: set[str] = field(default_factory=set)
    fixtures_requested: set[str] = field(default_factory=set)
    fixtures: dict[str, tuple[set[str], bool, Facts]] = field(default_factory=dict)  # name -> (deps, autouse, facts)


DYNAMIC_IMPORTS = {"import_module", "__import__", "importorskip", "run_module"}
PATCH_CALLS = {"patch", "setattr", "delattr", "setitem"}


def _const_str(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


TMP_PARAMS = {"tmp_path", "tmpdir", "tmp_path_factory", "tmp", "tmp_dir", "tmp_root"}


def _tmp_names(tree: ast.AST) -> set[str]:
    """Names that only ever hold a ``tmp_path``-derived value (fixture layouts, not the checkout)."""

    names = {
        a.arg
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        for a in n.args.args + n.args.kwonlyargs
        if "tmp" in a.arg.lower()
    } | TMP_PARAMS
    assigned: dict[str, list[ast.AST]] = collections.defaultdict(list)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned[t.id].append(n.value)
        elif (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None) or isinstance(
            n, ast.NamedExpr
        ):
            assigned[n.target.id].append(n.value)
    changed = True
    while changed:
        changed = False
        for name, values in assigned.items():
            if name in names:
                continue
            if all(_base_name(v) in names for v in values):
                names.add(name)
                changed = True
    return names


def _base_name(node: ast.AST) -> str | None:
    """Leftmost name of a path expression (``a / "b"``, ``a.joinpath(...)``, ``Path(a)``, ``a.parent``)."""

    while True:
        if isinstance(node, ast.BinOp):
            node = node.left
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                node = node.func.value
            elif node.args and _call_name(node.func) in {"Path", "str", "PurePosixPath", "PosixPath"}:
                node = node.args[0]
            else:
                return None
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return None


PATH_PARTS: dict[str, list[str]] = {}  # names bound to a checkout path chain in the file being read


def _path_chain(node: ast.AST, consts: dict[str, str]) -> list[str] | None:
    """String parts of a ``a / "b" / "c"`` chain (leftmost non-string base dropped).

    A name bound to an earlier chain (``DASH = ROOT / "dashboards"``) contributes its parts.
    """

    if isinstance(node, ast.Name) and node.id in PATH_PARTS:
        return list(PATH_PARTS[node.id])
    if (
        isinstance(node, ast.Call)
        and _call_name(node.func) in {"Path", "PurePosixPath"}
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.BinOp, ast.Name))
    ):
        return _path_chain(node.args[0], consts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_chain(node.left, consts)
        right = _const_str(node.right, consts)
        if right is None:
            return left  # ``ROOT / "dir" / name``: the directory is the subject
        return (left or []) + [right]
    value = _const_str(node, consts)
    return [value] if value is not None else None


def extract(source: str, module_name: str) -> Facts:
    facts = Facts()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        facts.unresolved.add(f"syntax error: {exc.msg}")
        return facts
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    consts: dict[str, str] = {}
    path_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                consts[node.targets[0].id] = value.value
            elif _is_path_expr(value, consts):
                path_names.add(node.targets[0].id)
    PATH_PARTS.clear()
    tmp_all = _tmp_names(tree)
    for _ in range(2):  # names bound to chains, including chains through other bound names
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, (ast.BinOp, ast.Call))
                and _base_name(node.value) not in tmp_all
            ):
                parts = _path_chain(node.value, consts)
                if parts and node.targets[0].id not in tmp_all:
                    PATH_PARTS.setdefault(node.targets[0].id, parts)
    package = module_name.rsplit(".", 1)[0] if "." in module_name else ""
    if module_name.endswith(".__init__"):
        package = module_name[: -len(".__init__")]

    def add_import(name: str, attrs: list[str]) -> None:
        if name == "tests" or name.startswith("tests."):
            facts.helpers.add(name)
            for attr in attrs:
                facts.helpers.add(f"{name}.{attr}")
            return
        if attrs:
            facts.attrs |= {(name, attr) for attr in attrs}
        else:
            facts.modules.add(name)

    _collect_scope(tree, facts, consts, docstrings, package, add_import, path_names)
    # Fixtures defined in this file (used when the file is a conftest).
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fixture, autouse, name = _fixture_info(node)
            if fixture:
                sub = Facts()
                _collect_scope(
                    node, sub, consts, docstrings, package, lambda n, a, sub=sub: _add_to(sub, n, a), path_names
                )
                deps = {arg.arg for arg in node.args.args + node.args.kwonlyargs} - {"self", "request"}
                facts.fixtures[name or node.name] = (deps, autouse, sub)
            if node.name.startswith("test") or fixture:
                facts.fixtures_requested |= {arg.arg for arg in node.args.args + node.args.kwonlyargs}
        if isinstance(node, ast.Call) and _call_name(node.func) == "usefixtures":
            for arg in node.args:
                value = _const_str(arg, consts)
                if value:
                    facts.fixtures_requested.add(value)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            for elt in ast.walk(node.value):
                value = _const_str(elt, consts)
                if value:
                    add_import(value, [])
    return facts


def _add_to(facts: Facts, name: str, attrs: list[str]) -> None:
    if name == "tests" or name.startswith("tests."):
        facts.helpers.add(name)
        facts.helpers |= {f"{name}.{a}" for a in attrs}
    elif attrs:
        facts.attrs |= {(name, a) for a in attrs}
    else:
        facts.modules.add(name)


def _fixture_info(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, bool, str | None]:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _call_name(target) == "fixture":
            autouse = False
            name = None
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "autouse" and isinstance(kw.value, ast.Constant):
                        autouse = bool(kw.value.value)
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name = str(kw.value.value)
            return True, autouse, name
    return False, False, None


def _is_path_expr(node: ast.AST, consts: dict[str, str]) -> bool:
    """A literal path: a ``/`` chain with string parts, or ``Path``/``str`` of one."""

    if isinstance(node, ast.Call) and _call_name(node.func) in {"Path", "str", "resolve", "PurePosixPath"}:
        inner = node.args[0] if node.args else (node.func.value if isinstance(node.func, ast.Attribute) else None)
        return inner is not None and _is_path_expr(inner, consts)
    chain = _path_chain(node, consts)
    return bool(chain) and isinstance(node, ast.BinOp)


def _collect_scope(
    root: ast.AST,
    facts: Facts,
    consts: dict[str, str],
    docstrings: set[int],
    package: str,
    add_import,
    path_names: set[str] = frozenset(),
) -> None:
    inner_chains = {id(n.left) for n in ast.walk(root) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    tmp_names = _tmp_names(root)
    chain_parts = {id(n.right) for n in ast.walk(root) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    chain_parts |= {
        id(n.left)
        for n in ast.walk(root)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and isinstance(n.left, ast.Constant)
    }
    for node in ast.walk(root):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_import(alias.name, [])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".") if package else []
                if node.level > 1:
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            add_import(base, [alias.name for alias in node.names if alias.name != "*"])
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            first = node.args[0] if node.args else None
            if name in DYNAMIC_IMPORTS:
                value = _const_str(first, consts) if first is not None else None
                if value is not None:
                    add_import(value, [])
                elif first is not None:
                    facts.unresolved.add(f"non-literal {name}({ast.unparse(first)[:80]})")
            elif name == "spec_from_file_location" and len(node.args) >= 2:
                target = node.args[1]
                chain = _path_chain(target, consts)
                if (
                    chain is None
                    and not (isinstance(target, ast.Name) and target.id in path_names)
                    and not _is_path_expr(target, consts)
                ):
                    facts.unresolved.add(f"non-literal spec_from_file_location path {ast.unparse(node.args[1])[:80]}")
            elif name in PATCH_CALLS and first is not None and isinstance(first, (ast.JoinedStr, ast.BinOp)):
                facts.unresolved.add(f"computed {name} target {ast.unparse(first)[:80]}")
            elif (
                name in {"insert", "append"}
                and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func.value) == "sys.path"
                and node.args
            ):
                target = node.args[-1]
                if isinstance(target, ast.Call) and _call_name(target.func) == "str" and target.args:
                    target = target.args[0]
                chain = _path_chain(target, consts)
                if chain:
                    facts.sys_path_roots.add("/".join(chain))
            elif name in {"join"} and isinstance(node.func, ast.Attribute):
                parts = [_const_str(arg, consts) for arg in node.args]
                strings = [p for p in parts if p is not None]
                if len(strings) >= 1:
                    facts.paths.add("/".join(strings))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in inner_chains:
            chain = _path_chain(node, consts)
            if chain and _base_name(node) not in tmp_names:
                facts.chain_paths.add("/".join(chain))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in chain_parts
        ):  # chain operands are judged as one path above
            value = node.value.strip()
            if value and len(value) <= 300 and "\n" not in value:
                _string_token(value, facts)
            for token in re.findall(r"[A-Za-z0-9_.\-/]+", node.value):  # "-m scripts.x", "scripts/x.py --flag"
                if token != value:
                    _string_token(token.strip("."), facts)


_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _string_token(value: str, facts: Facts) -> None:
    if _DOTTED.match(value):
        facts.weak_modules.add(value)
    if "/" in value or value.endswith((".py", ".sh", ".json", ".yaml", ".yml", ".md", ".toml")):
        facts.paths.add(value)


@dataclass
class Subject:
    ref: str  # LU path or dotted module
    lu_path: str
    owner: str
    status: str  # present | gone | unresolved
    detail: str


class Resolver:
    def __init__(self, lu: Path, mach: Path) -> None:
        self.lu_repo, self.m_repo = lu, mach
        self.lu = Blobs(lu, LU_SHA)
        self.m = Blobs(mach, MACHINERY_SHA)
        self.lu_files = set(git(lu, "ls-tree", "-r", "--name-only", LU_SHA).splitlines())
        self.m_files = set(git(mach, "ls-tree", "-r", "--name-only", MACHINERY_SHA).splitlines())
        self.snapshot_files = set(git(mach, "ls-tree", "-r", "--name-only", SNAPSHOT_SHA).splitlines())
        self.lu_dirs = {posixpath.dirname(p) for p in self.lu_files}
        for d in list(self.lu_dirs):
            while d:
                d = posixpath.dirname(d)
                self.lu_dirs.add(d)
        self.m_dirs = {posixpath.dirname(p) for p in self.m_files}
        for d in list(self.m_dirs):
            while d:
                d = posixpath.dirname(d)
                self.m_dirs.add(d)
        self.rename_map: dict[str, str] = json.loads(self.m.read(RENAME_MAP) or b"{}")
        # Keys the current map renamed away (for example kimi_admission -> retired_seat_admission)
        # keep the owner recorded by the newest earlier map version that has them.
        self.map_history: dict[str, tuple[str, str]] = {}
        for line in git(
            mach, "log", "--format=%H", MACHINERY_SHA, "--", RENAME_MAP, "packaging/machinery-move/rename-map.json"
        ).split():
            for name in (RENAME_MAP, "packaging/machinery-move/rename-map.json"):
                proc = _git_run(mach, "show", f"{line}:{name}")
                if proc.returncode == 0:
                    for key, owner in json.loads(proc.stdout).items():
                        self.map_history.setdefault(key, (owner, line[:8]))
        self.modules: dict[str, str] = {}  # dotted -> LU path (file or package __init__ or namespace dir)
        for path in self.lu_files:
            if not path.endswith(".py"):
                continue
            parts = path[:-3].split("/")
            if not all(p.isidentifier() for p in parts):
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            self.modules[".".join(parts)] = path
            for depth in range(1, len(parts)):
                self.modules.setdefault(".".join(parts[:depth]), "/".join(parts[:depth]))
        self.unqualified_tops = {name.split(".")[1] for name in self.modules if name.startswith("scripts.")}
        self.renames, self.deletions = self._history()
        self.defined_names = self._machinery_definitions()

    # -- machinery history -------------------------------------------------
    def _history(self) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
        renames: dict[str, tuple[str, str]] = {}
        deletions: dict[str, tuple[str, str]] = {}
        log = git(
            self.m_repo,
            "log",
            "-M",
            "--diff-filter=RD",
            "--name-status",
            "--format=@@%h",
            f"{SNAPSHOT_SHA}..{MACHINERY_SHA}",
        )
        commit = ""
        for line in log.splitlines():
            if line.startswith("@@"):
                commit = line[2:]
            elif line.startswith("R"):
                _, old, new = line.split("\t")
                renames.setdefault(old, (new, commit))
            elif line.startswith("D"):
                deletions.setdefault(line.split("\t")[1], ("", commit))
        # Low-similarity pairing inside each deleting commit.
        for old, (_, commit) in list(deletions.items()):
            pairs = git(self.m_repo, "show", "-M20%", "--diff-filter=R", "--name-status", "--format=", commit)
            for line in pairs.splitlines():
                bits = line.split("\t")
                if len(bits) == 3 and bits[1] == old:
                    renames.setdefault(old, (bits[2], commit))
        return renames, deletions

    def _machinery_definitions(self) -> dict[str, set[str]]:
        names: dict[str, set[str]] = collections.defaultdict(set)
        for path in self.m_files:
            if not path.endswith(".py") or not path.startswith("lu_machinery/"):
                continue
            text = self.m.text(path) or ""
            for match in re.finditer(
                r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)|^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]",
                text,
                re.M,
            ):
                names[match.group(1) or match.group(2)].add(path)
        return names

    def final_path(self, path: str) -> tuple[str | None, list[str]]:
        """Follow renames from a machinery path; return the present path or None."""

        trail: list[str] = []
        seen = set()
        while path not in self.m_files and path in self.renames and path not in seen:
            seen.add(path)
            new, commit = self.renames[path]
            trail.append(f"{path} -> {new} ({commit})")
            path = new
        if path in self.m_files:
            if trail:
                follow = git(
                    self.m_repo, "log", "--follow", "--diff-filter=R", "--format=%h", MACHINERY_SHA, "--", path
                ).split()
                trail.append(f"git log --follow --diff-filter=R: {' '.join(follow) or 'none'}")
            return path, trail
        return None, trail

    # -- owners ------------------------------------------------------------
    def owner(self, lu_path: str) -> str:
        if lu_path in self.rename_map:
            return self.rename_map[lu_path]
        if lu_path in self.map_history:
            return self.map_history[lu_path][0]
        owners = {o for k, o in self.rename_map.items() if k.startswith(lu_path + "/")}
        if len(owners) == 1:
            return owners.pop()
        return "mixed" if owners else "product"

    # -- subjects ----------------------------------------------------------
    def module_path(self, dotted: str) -> tuple[str, str] | None:
        """Longest LU module prefix (absolute or through pythonpath=scripts) and the rest."""

        candidates = [dotted]
        top = dotted.split(".")[0]
        if top in self.unqualified_tops and not dotted.startswith("scripts."):
            candidates.insert(0, "scripts." + dotted)
        for name in candidates:
            parts = name.split(".")
            for depth in range(len(parts), 0, -1):
                prefix = ".".join(parts[:depth])
                if prefix in self.modules:
                    return prefix, ".".join(parts[depth:])
        return None

    def subject_for_path(self, lu_path: str, ref: str) -> Subject:
        owner = self.owner(lu_path)
        if owner != "machinery":
            return Subject(ref, lu_path, owner, "present", f"{owner}-owned in LU")
        mpath = machinery_path(lu_path)
        if mpath in self.m_files or (lu_path not in self.lu_files and mpath in self.m_dirs):
            return Subject(ref, lu_path, owner, "present", f"present on machinery main as {mpath}")
        moved, trail = self.final_path(mpath)
        if moved:
            return Subject(ref, lu_path, owner, "present", "moved: " + "; ".join(trail))
        if lu_path not in self.lu_files:  # a directory
            members = [p for p in self.m_files if p.startswith(mpath + "/")]
            if members:
                return Subject(ref, lu_path, owner, "present", f"directory present ({len(members)} files)")
            # A moved directory: trace each machinery-owned LU file under it.
            moved_to = set()
            for member in sorted(p for p in self.lu_files if p.startswith(lu_path + "/")):
                if self.owner(member) == "machinery":
                    found, _ = self.final_path(machinery_path(member))
                    if found:
                        moved_to.add(posixpath.dirname(found))
            if moved_to:
                where = ", ".join(sorted(moved_to)[:3])
                return Subject(ref, lu_path, owner, "present", f"directory moved: its files are now under {where}")
            return Subject(ref, lu_path, owner, "unresolved", f"machinery directory {mpath} absent; not traced")
        if mpath not in self.snapshot_files:
            return Subject(ref, lu_path, owner, "unresolved", f"{mpath} never in the extraction snapshot")
        _, commit = self.deletions.get(mpath, ("", ""))
        if not commit:
            return Subject(ref, lu_path, owner, "unresolved", f"{mpath} absent with no deletion record")
        kind, why = DELETION_PATH_REASONS.get(mpath) or DELETION_REASONS.get(commit, ("unknown", ""))
        if kind == "retired":
            return Subject(ref, lu_path, owner, "gone", f"deleted in machinery {commit}: {why}")
        if kind in {"product-retained", "moved"}:
            return Subject(ref, lu_path, owner, "present", f"{kind} ({commit}): {why}")
        return Subject(ref, lu_path, owner, "unresolved", f"deleted in {commit} for an unrecorded reason")

    def resolve_facts(self, facts: Facts) -> tuple[list[Subject], list[str]]:
        subjects: dict[str, Subject] = {}
        unresolved = set(facts.unresolved)
        for dotted in facts.modules | facts.weak_modules:
            strong = dotted in facts.modules
            found = self.module_path(dotted)
            if found is None:
                if strong and (dotted.startswith("scripts.") or dotted.split(".")[0] in self.unqualified_tops):
                    unresolved.add(f"import of {dotted} resolves to no LU module")
                continue
            prefix = found[0]
            if prefix in {"scripts", "tests"} or prefix.startswith("tests."):
                continue
            if not strong and "." not in prefix:
                continue  # a dotted string reaching only a top-level package names nothing
            # Unqualified dotted strings only count when they reach past the top package.
            if (
                not strong
                and dotted.split(".")[0] in self.unqualified_tops
                and not dotted.startswith("scripts.")
                and "." not in prefix.replace("scripts.", "", 1)
            ):
                continue
            if self.modules[prefix] in self.lu_files or self.modules[prefix] in self.lu_dirs:
                subjects.setdefault(prefix, self.subject_for_path(self.modules[prefix], prefix))
        for base, attr in facts.attrs:
            found = self.module_path(f"{base}.{attr}")
            base_found = self.module_path(base)
            if found and not found[1] and (base_found is None or found[0] != base_found[0]):
                name = found[0]  # a submodule import
            else:
                found = base_found
                if found is None:
                    if base.startswith("scripts.") or base.split(".")[0] in self.unqualified_tops:
                        unresolved.add(f"import from {base} resolves to no LU module")
                    continue
                name = found[0]
            if name in {"scripts"} or name.startswith("tests"):
                continue
            subjects.setdefault(name, self.subject_for_path(self.modules[name], name))
        # Unqualified imports through a checkout directory the file puts on sys.path
        # (``sys.path.insert(0, str(ROOT / "scripts" / "lint"))``): every such resolution is a subject.
        for root in sorted(facts.sys_path_roots):
            if root == "scripts" or not root.startswith("scripts/"):
                continue
            prefix_name = root.replace("/", ".")
            for dotted in facts.modules | {base for base, _ in facts.attrs}:
                if dotted.startswith(("scripts.", "tests.")) or dotted == "tests":
                    continue
                name = f"{prefix_name}.{dotted}"
                found = self.module_path(name)
                if found and found[0] != prefix_name and found[0].startswith(prefix_name + "."):
                    subjects.setdefault(found[0], self.subject_for_path(self.modules[found[0]], found[0]))
        for raw, chained in [(p, False) for p in facts.paths] + [(p, True) for p in facts.chain_paths]:
            for path in self._path_candidates(raw, chained):
                subjects.setdefault(path, self.subject_for_path(path, path))
        # Attribute moves: a gone module's imported names defined elsewhere in machinery.
        for module, attr in facts.attrs:
            found = self.module_path(module)
            if not found:
                continue
            subject = subjects.get(found[0])
            if subject is None or subject.status != "gone" or attr in GENERIC_ATTRS or attr.startswith("__"):
                continue
            if self.module_path(f"{found[0]}.{attr}") and self.module_path(f"{found[0]}.{attr}")[0] != found[0]:
                continue  # a submodule, resolved on its own
            homes = sorted(self.defined_names.get(attr, ()))
            if homes:
                subject.status = "present"
                subject.detail += f"; imported name {attr!r} defined on machinery main in {', '.join(homes[:3])}"
        return sorted(subjects.values(), key=lambda s: s.ref), sorted(unresolved)

    def _path_candidates(self, raw: str, chained: bool = False) -> list[str]:
        raw = raw.strip().strip("/")
        raw = re.sub(r"^\./", "", raw)
        # ``ROOT / "scripts"`` is the import root put on sys.path (like the ``scripts`` package), not a subject.
        if not raw or raw == "scripts" or raw.startswith(("http", "tmp", "/")):
            return []
        out = []
        parts = raw.split("/")
        for start in range(len(parts)):
            candidate = "/".join(parts[start:])
            if candidate in self.lu_files or (candidate in self.lu_dirs and ("/" in candidate or chained)):
                if candidate.startswith("tests/"):
                    return []
                out.append(candidate)
                break
            if start == 0:
                continue
            break
        return out


@dataclass
class FileResult:
    path: str
    status: str
    subjects: list[Subject]
    unresolved: list[str]
    users: list[str] = field(default_factory=list)
    evidence: dict | None = None


class Triage:
    def __init__(self, resolver: Resolver, census: dict) -> None:
        self.r = resolver
        self.census = census
        self.facts: dict[str, Facts] = {}

    def module_name(self, path: str) -> str:
        return path[:-3].replace("/", ".")

    def file_facts(self, path: str) -> Facts:
        if path not in self.facts:
            text = self.r.lu.text(path)
            self.facts[path] = (
                Facts(unresolved={"unreadable"}) if text is None else extract(text, self.module_name(path))
            )
        return self.facts[path]

    def helper_path(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        for depth in range(len(parts), 0, -1):
            prefix = "/".join(parts[:depth])
            for candidate in (prefix + ".py", prefix + "/__init__.py"):
                if candidate in self.r.lu_files:
                    return candidate
        return None

    def conftests(self, path: str) -> list[str]:
        out = []
        directory = posixpath.dirname(path)
        while directory.startswith("tests"):
            candidate = f"{directory}/conftest.py"
            if candidate in self.r.lu_files and candidate != path:
                out.append(candidate)
            directory = posixpath.dirname(directory)
        return out

    def closure(self, path: str) -> tuple[Facts, list[str]]:
        """Merged facts of a file, its imported helpers and the conftest fixtures it uses."""

        merged = Facts()
        seen: set[str] = set()
        trail: list[str] = []
        queue = [path]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            facts = self.file_facts(current)
            _merge(merged, facts)
            if current != path:
                trail.append(current)
            for helper in facts.helpers:
                helper_file = self.helper_path(helper)
                if helper_file and not helper_file.endswith("__init__.py"):
                    queue.append(helper_file)
            # Fixtures: requested names resolved through this file and its conftest chain.
            wanted = set(facts.fixtures_requested)
            for conftest in [current, *self.conftests(current)]:
                cfacts = self.file_facts(conftest)
                is_root = conftest == "tests/conftest.py"
                pending = set(wanted) | {n for n, (_, auto, _) in cfacts.fixtures.items() if auto and not is_root}
                done: set[str] = set()
                while pending:
                    name = pending.pop()
                    if name in done or name not in cfacts.fixtures:
                        continue
                    done.add(name)
                    deps, _, sub = cfacts.fixtures[name]
                    _merge(merged, sub)
                    pending |= deps
                    for helper in sub.helpers:
                        helper_file = self.helper_path(helper)
                        if helper_file and not helper_file.endswith("__init__.py"):
                            queue.append(helper_file)
                    if conftest != current:
                        trail.append(f"{conftest}::{name}")
        merged.fixtures = {}
        return merged, sorted(set(trail))

    def classify_python(self, path: str) -> FileResult:
        facts, trail = self.closure(path)
        subjects, unresolved = self.r.resolve_facts(facts)
        dropped = HAND_REVIEW.get(path, {})
        for subject in subjects:
            if subject.ref in dropped:
                subject.status = "not-a-subject"
                subject.detail = "hand review: " + dropped[subject.ref]
        subjects_checked = [s for s in subjects if s.status != "not-a-subject"]
        result = FileResult(path, PRESENT, subjects, unresolved)
        result.users = trail
        if unresolved:
            return result
        if not subjects_checked:
            result.unresolved = ["empty subject set"]
            return result
        if any(s.status != "gone" for s in subjects_checked):
            for s in subjects_checked:
                if s.status == "unresolved":
                    result.unresolved.append(f"{s.ref}: {s.detail}")
            return result
        result.status = GONE
        return result

    def run(self) -> dict[str, FileResult]:
        files = self.census["files"]
        port_first = sorted(p for p, e in files.items() if e["disposition"] == "port-first")
        python = [p for p in port_first if p.endswith(".py")]
        data = [p for p in port_first if not p.endswith(".py")]
        results = {p: self.classify_python(p) for p in python}
        # Users of support modules and conftests among port-first files.
        all_tests = [p for p in self.r.lu_files if p.startswith("tests/") and p.endswith(".py")]
        for path in python:
            res = results[path]
            if res.status != GONE:
                continue
            base = posixpath.basename(path)
            if base.startswith("test_"):
                continue
            if base == "conftest.py":
                users = [p for p in all_tests if p.startswith(posixpath.dirname(path) + "/") and p != path]
            else:
                dotted = self.module_name(path)
                users = [p for p in all_tests if p != path and dotted in (self.r.lu.text(p) or "")]
                stem = dotted.rsplit(".", 1)[1]
                users += [
                    p
                    for p in all_tests
                    if p != path
                    and p not in users
                    and re.search(
                        rf"\bimport\s+{re.escape(stem)}\b|from\s+\.+{re.escape(stem)}\b|"
                        rf"from\s+\.+\s+import\s+[^\n]*\b{re.escape(stem)}\b",
                        self.r.lu.text(p) or "",
                    )
                ]
            res.users = sorted(users)
            if any(results.get(u, FileResult(u, PRESENT, [], [])).status not in (GONE,) for u in users):
                res.status = PRESENT
                res.unresolved.append("support module used by a file that is not SUBJECT-GONE")
        for path in data:
            entry = files[path]
            users = set(re.findall(r"port-first file (tests/\S+\.py)", " ".join(entry.get("missing_behaviours", []))))
            base = posixpath.basename(path)
            needle = base if len(base) >= 8 and base not in {"__init__.py", "README.md", "conftest.py"} else None
            if needle:
                users |= {p for p in all_tests if needle in (self.r.lu.text(p) or "")}
            subjects = [
                Subject(u, u, "test", "user", results[u].status if u in results else "not port-first")
                for u in sorted(users)
            ]
            res = FileResult(path, PRESENT, subjects, [], users=sorted(users))
            if not users:
                res.unresolved = ["no user found"]
            elif all(u in results and results[u].status == GONE for u in users):
                res.status = GONE
            results[path] = res
        for path, res in results.items():
            if res.status == GONE:
                res.evidence = self.retirement_evidence(res)
                if res.evidence["callers"]:
                    res.status = PRESENT
                    res.unresolved.append(
                        "subject removed from machinery but callers remain: " + "; ".join(res.evidence["callers"][:5])
                    )
                    res.evidence = None
                elif path in PROTECTED:
                    res.status = GONE_PROTECTED
                    res.evidence["protected"] = PROTECTED[path]
        return results

    def retirement_evidence(self, res: FileResult) -> dict:
        """References to each gone subject outside its own file, tests and the move inventories."""

        callers: list[str] = []
        checked: list[str] = []
        subjects = list({s.lu_path: s for s in res.subjects if s.status == "gone"}.values())
        if not subjects:  # data path: evidence of its users
            subjects = [s for u in res.users for s in self.facts_subjects(u)]
        for s in subjects:
            mpath = machinery_path(s.lu_path)
            stem = posixpath.splitext(posixpath.basename(s.lu_path))[0]
            dotted = s.lu_path[:-3].replace("/", ".") if s.lu_path.endswith(".py") else s.lu_path
            mdotted = mpath[:-3].replace("/", ".") if mpath.endswith(".py") else mpath
            unq = dotted[len("scripts.") :] if dotted.startswith("scripts.") else dotted
            patterns = sorted({dotted, mdotted, unq, s.lu_path, mpath, stem})
            for repo, sha, own in ((self.r.lu_repo, LU_SHA, s.lu_path), (self.r.m_repo, MACHINERY_SHA, mpath)):
                hits = _grep(repo, sha, patterns)
                label = "LU" if repo == self.r.lu_repo else "machinery"
                classes: dict[str, list[str]] = collections.defaultdict(list)
                for hit in hits:
                    if hit == own:
                        classes["the subject itself"].append(hit)
                    elif hit.startswith("tests/"):
                        classes["tests"].append(hit)
                    elif hit.startswith(INVENTORY_PATHS):
                        classes["move inventories"].append(hit)
                    elif repo == self.r.lu_repo and self.r.owner(hit) == "machinery":
                        # LU's machinery-owned copies are replaced by machinery at the switch;
                        # their machinery counterparts are searched at MACHINERY_SHA.
                        classes["LU machinery-owned copies (superseded)"].append(hit)
                    else:
                        classes["callers"].append(hit)
                        callers.append(f"{label} {hit}")
                summary = "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in sorted(classes.items())) or "no hits"
                checked.append(f"{label} {sha[:8]} git grep -l -F -w -e {' -e '.join(patterns)} -> {summary}")
        return {
            "subjects_removed": [f"{s.lu_path}: {s.detail}" for s in subjects],
            "supported_callers": callers or "none",
            "compatibility_migration_rollback": "none: no shim, migration or rollback path references the subject"
            if not callers
            else "see callers",
            "historical_readers": "none: no non-test reader of the subject's paths or names at either frozen SHA"
            if not callers
            else "see callers",
            "searches": checked,
            "callers": callers,
        }

    def facts_subjects(self, path: str) -> list[Subject]:
        res = self.classify_python(path)
        return [s for s in res.subjects if s.status == "gone"]


def _grep(repo: Path, sha: str, patterns: list[str]) -> list[str]:
    args = ["grep", "-l", "-F", "-w"]
    for pattern in patterns:
        args += ["-e", pattern]
    proc = _git_run(repo, *args, sha)
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.decode(errors="replace"))
    return sorted({line.split(":", 1)[1] for line in proc.stdout.decode().splitlines()})


def _merge(into: Facts, other: Facts) -> None:
    into.modules |= other.modules
    into.attrs |= other.attrs
    into.weak_modules |= other.weak_modules
    into.paths |= other.paths
    into.chain_paths |= other.chain_paths
    into.sys_path_roots |= other.sys_path_roots
    into.unresolved |= other.unresolved


def area(path: str) -> str:
    parts = path.split("/")
    return parts[1] if len(parts) > 2 else "root"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lu-repo", type=Path, required=True)
    parser.add_argument("--machinery-repo", type=Path, required=True)
    parser.add_argument("--write", type=Path, help="write the annotated census here")
    parser.add_argument("--report", type=Path, help="write the full per-file report (JSON) here")
    args = parser.parse_args()
    resolver = Resolver(args.lu_repo, args.machinery_repo)
    census_raw = resolver.m.read(CENSUS)
    if census_raw is None:
        raise SystemExit(f"{CENSUS} missing at {MACHINERY_SHA}")
    census = json.loads(census_raw)
    results = Triage(resolver, census).run()
    counts = collections.Counter(r.status for r in results.values())
    kinds = collections.Counter((r.status, "py" if p.endswith(".py") else "data") for p, r in results.items())
    print(f"LU {LU_SHA} machinery {MACHINERY_SHA}: {len(results)} port-first paths")
    for status in (GONE, GONE_PROTECTED, PRESENT):
        print(f"  {status}: {counts[status]} (py {kinds[(status, 'py')]}, data {kinds[(status, 'data')]})")
    by_area = collections.Counter(area(p) for p, r in results.items() if r.status == PRESENT)
    print("  SUBJECT-PRESENT by area: " + ", ".join(f"{a} {n}" for a, n in by_area.most_common()))
    if args.write:
        for path, res in results.items():
            entry = census["files"][path]
            entry["subject_status"] = res.status
            entry["subjects"] = [f"{s.ref} [{s.status}: {s.detail}]" for s in res.subjects]
            entry["subjects"] += [f"unresolved: {u}" for u in res.unresolved]
            if res.evidence is not None:
                entry["retirement_evidence"] = {k: v for k, v in res.evidence.items() if k != "callers"}
        args.write.write_text(json.dumps(census, indent=1, ensure_ascii=True, sort_keys=True) + "\n")
    if args.report:
        report = {
            p: {
                "status": r.status,
                "subjects": [s.__dict__ for s in r.subjects],
                "unresolved": r.unresolved,
                "users": r.users,
                "evidence": r.evidence,
            }
            for p, r in sorted(results.items())
        }
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

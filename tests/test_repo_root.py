"""Unit and integration tests for repository root resolver and primary checkout anchoring."""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common import repo_root as rr
from lu_machinery.common.repo_root import main_checkout_root, project_interpreter
from lu_machinery.orchestration import worktree_claims

REPO = Path(__file__).resolve().parents[1]


def test_main_checkout_root_resolves_primary_checkout_from_worktree(tmp_path):
    """Verify that main_checkout_root correctly identifies primary repo root from a worktree."""
    main_repo = tmp_path / "main"
    worktree = main_repo / ".worktrees" / "dispatch" / "agy" / "task-123"
    git_dir = main_repo / ".git" / "worktrees" / "task-123"

    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    assert main_checkout_root(worktree) == main_repo


@pytest.fixture
def wt_layout(tmp_path) -> tuple[Path, Path]:
    """Sets up a mock primary repo and a mock worktree layout.

    Returns a tuple of (mock_primary_root, mock_worktree_root).
    """
    main_repo = tmp_path / "main"
    worktree = main_repo / ".worktrees" / "dispatch" / "agy" / "task-123"
    git_dir = main_repo / ".git" / "worktrees" / "task-123"

    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    # Copy the bridge and common module code files to the mock worktree
    src_root = Path(__file__).resolve().parents[1]

    # Create empty init for the bridge package to avoid loading other bridge modules
    bridge_init = worktree / "lu_machinery" / "ai_agent_bridge" / "__init__.py"
    bridge_init.parent.mkdir(parents=True, exist_ok=True)
    bridge_init.write_text("", encoding="utf-8")

    files_to_copy = [
        "lu_machinery/common/__init__.py",
        "lu_machinery/common/bridge_paths.py",
        "lu_machinery/common/repo_root.py",
        "lu_machinery/common/github_client.py",
        "lu_machinery/ai_agent_bridge/_config.py",
        "lu_machinery/ai_agent_bridge/_env.py",
        "lu_machinery/ai_agent_bridge/_dispatch_wrappers.py",
        "lu_machinery/ai_agent_bridge/_monitor_cache.py",
        "lu_machinery/review/__init__.py",
        "lu_machinery/review/verdict_parser.py",
    ]
    for f in files_to_copy:
        dest = worktree / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((src_root / f).read_text(encoding="utf-8"), encoding="utf-8")

    return main_repo, worktree


def test_bridge_config_state_paths_resolve_to_primary_in_worktree(wt_layout):
    """Verify that _config.py run from a worktree layout resolves state paths to the primary root.

    lu_machinery derives no state anchor (C-4843), so the primary is configured, as installed
    machinery has it; the worktree stays the working directory.
    """
    main_repo, worktree = wt_layout

    # Run subprocess to check DB_PATH and PID_DIR
    script = "from lu_machinery.ai_agent_bridge._config import DB_PATH, PID_DIR; print(f'{DB_PATH};{PID_DIR}')"
    env = os.environ.copy()
    env.pop("AB_DB_PATH", None)  # Exercise the default path, not the suite's isolated override.
    env["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] = str(main_repo)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(worktree),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    db_path_str, pid_dir_str = proc.stdout.strip().split(";")

    expected_db_path = main_repo / ".mcp" / "servers" / "message-broker" / "messages.db"
    expected_pid_dir = main_repo / ".mcp" / "servers" / "message-broker" / "pids"

    assert Path(db_path_str).resolve() == expected_db_path.resolve()
    assert Path(pid_dir_str).resolve() == expected_pid_dir.resolve()


def _write_venv_python(checkout: Path, name: str = "python") -> Path:
    interpreter = checkout / ".venv" / "bin" / name
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    interpreter.chmod(0o755)
    return interpreter


def _link_worktree(primary: Path, worktree: Path) -> None:
    git_dir = primary / ".git" / "worktrees" / "task"
    git_dir.mkdir(parents=True, exist_ok=True)
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")


def test_project_interpreter_uses_an_executable_override(tmp_path):
    """A valid LEARN_UKRAINIAN_PYTHON wins over the primary and the worktree."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    _write_venv_python(primary)
    _write_venv_python(worktree)
    override = tmp_path / "override-python"
    override.write_text("", encoding="utf-8")
    override.chmod(0o755)

    assert project_interpreter(worktree, env={"LEARN_UKRAINIAN_PYTHON": str(override)}) == override


def test_project_interpreter_refuses_an_invalid_override_without_fallback(tmp_path):
    """An invalid override is a refusal. The primary venv is not a fallback."""
    primary = tmp_path / "primary"
    primary.mkdir()
    primary_python = _write_venv_python(primary)
    missing = tmp_path / "missing-python"
    unexecutable = tmp_path / "plain-file"
    unexecutable.write_text("", encoding="utf-8")

    with pytest.raises(rr.MachineryConfigError, match="must be an absolute path"):
        project_interpreter(primary, env={"LEARN_UKRAINIAN_PYTHON": "python"})
    for value in (str(missing), str(unexecutable)):
        with pytest.raises(FileNotFoundError, match="is not an executable file"):
            project_interpreter(primary, env={"LEARN_UKRAINIAN_PYTHON": value})
    assert project_interpreter(primary, env={}) == primary_python


def test_project_interpreter_refuses_sys_executable_when_no_primary_venv_exists(tmp_path, monkeypatch):
    """The removed sys.executable fallback stays a refusal, including for a foreign venv."""
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(checkout, env={})
    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(Path("/no-such-checkout"), env={})


def test_project_interpreter_refuses_a_worktree_venv_when_the_primary_has_none(tmp_path, monkeypatch):
    """A worktree-local .venv is not the interpreter when the primary has none."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    local = _write_venv_python(worktree)
    monkeypatch.setattr(sys, "executable", str(local))

    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(worktree, env={})


def test_project_interpreter_prefers_the_primary_checkout_venv(tmp_path, monkeypatch):
    """The primary checkout's .venv/bin/python wins when the worktree also has one."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    primary_python = _write_venv_python(primary)
    local = _write_venv_python(worktree)
    monkeypatch.setattr(sys, "executable", str(local))

    assert project_interpreter(worktree, env={}) == primary_python


def test_project_interpreter_uses_the_primary_venv_when_the_checkout_has_none(tmp_path, monkeypatch):
    """A linked worktree with no .venv uses the primary checkout's interpreter."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    primary_python = _write_venv_python(primary)
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))

    assert project_interpreter(worktree, env={}) == primary_python


def test_project_interpreter_refuses_a_non_executable_primary_venv(tmp_path):
    """A primary .venv/bin/python that is not executable is a refusal."""
    primary = tmp_path / "primary"
    primary.mkdir()
    interpreter = primary / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(primary, env={})


# --------------------------------------------------------------------------- C-4843 root contract, step 1
#
# Installed lu_machinery has no implicit product and no implicit state anchor; only the
# in-tree source copy keeps its checkout defaults. Explicit selection needs no curriculum
# marker, an invalid selection never falls through, decoys in the working directory are
# never consulted, and resolution creates nothing.

_ROOT_ENV = (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV)
_copy_ids = itertools.count()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ROOT_ENV:
        monkeypatch.delenv(name, raising=False)


def _primary_checkout(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _decoy_cwd(path: Path) -> Path:
    """A directory that looks like a product primary holding dispatch state."""
    marker = path / rr.PRODUCT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("decoy: true\n", encoding="utf-8")
    (path / "batch_state" / "tasks").mkdir(parents=True)
    (path / "batch_state" / "fleet-comms" / "v1").mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _tree(root: Path) -> list[tuple[str, str]]:
    """Every entry under ``root`` (not following symlinks), with its kind."""
    entries = []
    for directory, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(directory, name)
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
            entries.append((path.relative_to(root).as_posix(), kind))
    return sorted(entries)


def _load_copy(monkeypatch: pytest.MonkeyPatch, package_dir: Path):
    """Load a copy of this resolver from ``package_dir/common/repo_root.py`` (as hooks load it, by file).

    The copy is registered in ``sys.modules`` for the test only (dataclasses look the module up).
    """
    target = package_dir / "common" / "repo_root.py"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rr.__file__, target)
    spec = importlib.util.spec_from_file_location(f"_repo_root_copy_{next(_copy_ids)}", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_installed_package_refuses_product_and_state_without_anchors(unconfigured, tmp_path, monkeypatch):
    """Defect class: installed machinery inventing a product or a state anchor from its own location or cwd."""
    assert rr.in_tree_source() is False
    assert rr.machinery_root().name == rr.INSTALLED_PACKAGE_NAME
    decoy = _decoy_cwd(tmp_path / "decoy")
    monkeypatch.chdir(decoy)
    with pytest.raises(rr.ProductRootError, match=rr.PRODUCT_ROOT_ENV):
        rr.product_root()
    for resolve in (
        rr.state_root,
        rr.primary_root,
        rr.machinery_roots,
        lambda: rr.state_root(decoy),
        lambda: rr.primary_root(decoy),
        lambda: rr.state_root(REPO),
    ):
        with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
            resolve()
    # Consumers that catch the resolver's ValueError keep refusing.
    with pytest.raises(worktree_claims.ControlPlaneError, match=rr.PRIMARY_ROOT_ENV):
        worktree_claims.public_primary_root()
    # A state root alone does not stand in for the primary anchor.
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(_primary_checkout(tmp_path / "primary")))
    with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
        rr.state_root()


def test_the_installed_package_resolves_its_configured_anchors(unconfigured, tmp_path, monkeypatch):
    """Defect class: a configured anchor ignored, or replaced by a cwd decoy, in the installed package."""
    monkeypatch.chdir(_decoy_cwd(tmp_path / "decoy"))
    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "kube-dojo"
    product.mkdir()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    assert rr.product_root() == product
    assert rr.state_root() == rr.primary_root() == primary
    assert worktree_claims.public_primary_root() == primary
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary))
    assert rr.state_root() == primary
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(tmp_path / "decoy"))
    with pytest.raises(rr.MachineryConfigError, match="state cutover"):
        rr.state_root()


@pytest.mark.parametrize("in_tree", [False, True], ids=["installed", "in-tree"])
def test_an_explicit_product_needs_no_curriculum_marker(unconfigured, tmp_path, monkeypatch, in_tree):
    """Defect class: a non-curriculum product (kube-dojo) refused because it lacks Ukrainian curriculum data."""
    monkeypatch.setattr(rr, "_IN_TREE_SOURCE", in_tree)
    product = tmp_path / "kube-dojo"
    product.mkdir()
    assert not (product / rr.PRODUCT_MARKER).exists()
    assert rr.product_root(product) == product
    assert rr.product_root(str(product)) == product
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    assert rr.product_root() == product
    # Returned as given: a release snapshot link stays the snapshot path.
    snapshot = tmp_path / "current"
    snapshot.symlink_to(product, target_is_directory=True)
    assert rr.product_root(snapshot) == snapshot


@pytest.mark.parametrize("bad", ["relative", "missing", "file", "nul", "empty", "bytes"])
def test_an_invalid_explicit_product_never_falls_through(unconfigured, tmp_path, monkeypatch, bad):
    """Defect class: a mistyped selection silently replaced by the environment or the checkout default."""
    fallback = tmp_path / "configured"
    fallback.mkdir()
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    value = {
        "relative": "kube-dojo",
        "missing": str(tmp_path / "missing"),
        "file": str(tmp_path / "a-file"),
        "nul": f"{tmp_path}/x\x00y",
        "empty": "",
        "bytes": os.fsencode(fallback),
    }[bad]
    # An in-tree copy inside a real product checkout: the derived default would succeed.
    product = tmp_path / "product"
    (product / rr.PRODUCT_MARKER).parent.mkdir(parents=True)
    (product / rr.PRODUCT_MARKER).write_text("", encoding="utf-8")
    in_tree = _load_copy(monkeypatch, product / "scripts")
    assert in_tree.in_tree_source() is True
    assert in_tree.product_root() == product
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(fallback))
    for module in (rr, in_tree):
        with pytest.raises(module.ProductRootError):
            module.product_root(value)
    if bad not in ("empty", "bytes", "nul"):  # empty means unset; the OS refuses NUL and bytes in os.environ
        monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, value)
        for module in (rr, in_tree):
            with pytest.raises(module.ProductRootError, match=rr.PRODUCT_ROOT_ENV):
                module.product_root()


def test_the_mode_comes_from_the_package_directory_name_not_the_surroundings(unconfigured, tmp_path, monkeypatch):
    """Defect class: a copy inside a product checkout losing its default, or a decoy enabling one."""
    monkeypatch.chdir(_decoy_cwd(tmp_path / "decoy"))
    # Installed-name copy inside a directory that carries every product and state marker.
    staged = _decoy_cwd(tmp_path / "site-packages-ish")
    installed = _load_copy(monkeypatch, staged / rr.INSTALLED_PACKAGE_NAME)
    assert installed.in_tree_source() is False
    with pytest.raises(installed.ProductRootError):
        installed.product_root()
    with pytest.raises(installed.StateRootError):
        installed.state_root()
    # The in-tree copy keeps its established checkout defaults.
    product = _decoy_cwd(tmp_path / "product")
    in_tree = _load_copy(monkeypatch, product / "scripts")
    assert in_tree.in_tree_source() is True
    assert in_tree.product_root() == product
    assert in_tree.state_root() == in_tree.primary_root() == product
    # A package directory symlinked into the product keeps the in-tree name it is loaded under.
    linked = _decoy_cwd(tmp_path / "linked-product")
    (linked / "scripts").symlink_to(staged / rr.INSTALLED_PACKAGE_NAME, target_is_directory=True)
    assert _load_copy(monkeypatch, linked / "scripts").in_tree_source() is True
    # An in-tree copy outside any product still refuses rather than consulting cwd.
    stray = _load_copy(monkeypatch, tmp_path / "stray" / "scripts")
    with pytest.raises(stray.ProductRootError, match="not a product checkout"):
        stray.product_root()


def test_resolution_creates_nothing(unconfigured, tmp_path, monkeypatch):
    """Defect class: a resolver that creates state directories, files or databases as a side effect."""
    decoy = _decoy_cwd(tmp_path / "decoy")
    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "product"
    product.mkdir()
    # An in-tree product checkout without state yet: its defaults must not create any.
    in_tree_product = _primary_checkout(tmp_path / "in-tree-product")
    (in_tree_product / rr.PRODUCT_MARKER).parent.mkdir(parents=True)
    (in_tree_product / rr.PRODUCT_MARKER).write_text("", encoding="utf-8")
    in_tree = _load_copy(monkeypatch, in_tree_product / "scripts")
    monkeypatch.chdir(decoy)
    watched = (tmp_path, REPO / rr.INSTALLED_PACKAGE_NAME / "common")
    before = [_tree(root) for root in watched]
    repo_top = sorted(os.listdir(REPO))

    def every_resolution() -> None:
        for module in (rr, in_tree):
            for resolve, args in (
                (module.product_root, ()),
                (module.product_root, (product,)),
                (module.product_root, (tmp_path / "missing",)),
                (module.state_root, ()),
                (module.primary_root, ()),
                (module.machinery_roots, ()),
                (module.machinery_path, ("config/model_catalog.yaml",)),
            ):
                with contextlib.suppress(ValueError, FileNotFoundError):
                    resolve(*args)

    every_resolution()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    every_resolution()
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary))
    every_resolution()
    assert [_tree(root) for root in watched] == before
    assert sorted(os.listdir(REPO)) == repo_top
    for root in (primary, product, in_tree_product):
        assert not (root / "batch_state").exists()


def test_package_imports_and_help_work_with_nothing_configured(tmp_path):
    """Defect class: a missing anchor breaking import or ``--help`` instead of the operation that needs it."""
    decoy = _decoy_cwd(tmp_path / "decoy")
    env = {key: value for key, value in os.environ.items() if key not in (*_ROOT_ENV, "LU_TASKS_DIR", "PYTHONPATH")}
    env["PYTHONPATH"] = str(REPO)
    before = _tree(decoy)
    probe = (
        "from lu_machinery.common import repo_root as rr\n"
        "for resolve in (rr.product_root, rr.state_root):\n"
        "    try:\n"
        "        resolve()\n"
        "    except rr.MachineryConfigError as exc:\n"
        "        print(type(exc).__name__)\n"
    )
    imported = subprocess.run(
        [sys.executable, "-P", "-c", probe], cwd=decoy, env=env, capture_output=True, text=True, timeout=120
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.split() == ["ProductRootError", "StateRootError"]
    helped = subprocess.run(
        [sys.executable, "-P", "-m", "lu_machinery.dispatch", "--help"],
        cwd=decoy,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert helped.returncode == 0, helped.stderr
    assert "usage" in helped.stdout.lower()
    assert _tree(decoy) == before
